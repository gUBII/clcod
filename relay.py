#!/usr/bin/env python3
"""
relay.py — Watches clcodgemmix.txt and routes new messages to Codex and Gemini
via their CLI tools. Claude's responses are handled by the active Claude Code session.

Usage:
    python3 relay.py                          # default log path
    python3 relay.py --log /path/to/log.txt   # custom log
"""

import asyncio
import argparse
import fcntl
import os
import re
import sys
import time

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(SCRIPT_DIR, "clcodgemmix.txt")
LOCK_FILE   = os.path.join(SCRIPT_DIR, "speaker.lock")
POLL_SEC    = 0.5   # how often to check for new content
TIMEOUT_SEC = 60    # max seconds to wait for a CLI response
CONTEXT_LEN = 6000  # chars of log tail sent as context
LOCK_TTL    = 90    # seconds before a stale lock is released

# All three agents. Claude is included — works when not nested inside Claude Code.
AGENTS = ("CLAUDE", "CODEX", "GEMINI")

PROMPT_TEMPLATE = (
    "You are {name}, one of three AI agents (Claude, Codex, Gemini) sharing a "
    "real-time terminal chat room.  Below is the recent conversation log.  "
    "Reply naturally as yourself in 2-5 sentences.  "
    "Do NOT prefix your reply with your name or a [TAG].\n\n{context}"
)


# ── speaker lock (anti-collision) ─────────────────────────────────────────────

def acquire_lock(owner: str) -> bool:
    """Try to claim the speaker lock. Returns True if acquired."""
    try:
        # If lock exists and is fresh, someone else is speaking
        if os.path.exists(LOCK_FILE):
            age = time.time() - os.path.getmtime(LOCK_FILE)
            if age < LOCK_TTL:
                return False
        with open(LOCK_FILE, "w") as f:
            f.write(f"{owner}:{time.time()}")
        return True
    except OSError:
        return False

def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass

def activity_jitter(content: str) -> float:
    """Adaptive jitter: short if conversation is busy, longer if quiet."""
    recent = content[-500:]
    msg_count = recent.count("\n[")
    if msg_count > 4:
        return 0.5   # busy — minimal wait
    elif msg_count > 2:
        return 1.0
    else:
        return 2.0   # quiet — give humans time to respond


# ── helpers ───────────────────────────────────────────────────────────────────

def last_speaker(text: str) -> str:
    """Return the last [SPEAKER] tag in the log."""
    tags = re.findall(r"^\[([A-Z]+)\]", text, re.MULTILINE)
    return tags[-1] if tags else ""


async def append_reply(lock: asyncio.Lock, path: str, speaker: str, text: str):
    """Atomically append a tagged reply to the log."""
    entry = f"\n[{speaker}]\n{text.strip()}\n"
    async with lock:
        with open(path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(entry)
            fcntl.flock(f, fcntl.LOCK_UN)


# ── CLI output parsers ────────────────────────────────────────────────────────

def parse_codex(raw: str) -> str:
    """Extract the actual reply from `codex exec` stdout.

    Codex prints:
        <header block>
        user
        <prompt>
        mcp startup: ...
        thinking
        <thought>
        codex
        <RESPONSE>
        tokens used
        <number>
        <RESPONSE repeated>

    Strategy: grab lines between the "codex" marker and "tokens used".
    """
    lines = raw.strip().splitlines()
    response: list[str] = []
    capture = False
    for line in lines:
        if line.strip() == "codex":
            capture = True
            continue
        if capture:
            if line.strip().startswith("tokens used"):
                break
            response.append(line)
    text = "\n".join(response).strip()
    if text:
        return text
    # Fallback: last non-metadata line
    skip = {
        "OpenAI", "--------", "workdir:", "model:", "provider:", "approval:",
        "sandbox:", "reasoning", "session id:", "user", "mcp startup:",
        "thinking", "codex", "tokens used",
    }
    for line in reversed(lines):
        s = line.strip()
        if s and not any(s.startswith(k) for k in skip) and not s.startswith("**"):
            try:
                int(s.replace(",", ""))   # skip bare numbers (token count)
                continue
            except ValueError:
                return s
    return ""


def parse_gemini(raw: str) -> str:
    """Strip the 'Loaded cached credentials.' preamble from gemini output."""
    lines = raw.strip().splitlines()
    return "\n".join(l for l in lines if "Loaded cached" not in l).strip()


# ── agent callers ─────────────────────────────────────────────────────────────

async def call_codex(prompt: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "codex", "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ephemeral",
        "-C", SCRIPT_DIR,
        prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SEC)
    return parse_codex(stdout.decode("utf-8", errors="replace"))


async def call_gemini(prompt: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gemini", "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SEC)
    return parse_gemini(stdout.decode("utf-8", errors="replace"))


async def call_claude(prompt: str) -> str:
    """Call claude -p. Works when NOT nested inside a Claude Code session."""
    env = {**os.environ, "CLAUDECODE": ""}  # unset nesting guard
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_SEC)
    raw = stdout.decode("utf-8", errors="replace").strip()
    # Strip any leading/trailing tool-use noise — claude -p is usually clean
    return raw


CALLERS = {
    "CLAUDE": call_claude,
    "CODEX":  call_codex,
    "GEMINI": call_gemini,
}


# ── routing ───────────────────────────────────────────────────────────────────

async def route_to(name: str, prompt: str, log_path: str, lock: asyncio.Lock):
    """Call one agent and append its reply."""
    try:
        reply = await CALLERS[name](prompt)
        if reply:
            await append_reply(lock, log_path, name, reply)
            print(f"  {name} replied ({len(reply)} chars)", flush=True)
        else:
            print(f"  {name}: empty reply", flush=True)
    except asyncio.TimeoutError:
        print(f"  {name}: timed out ({TIMEOUT_SEC}s)", flush=True)
    except Exception as e:
        print(f"  {name}: error — {e}", flush=True)


# ── main loop ─────────────────────────────────────────────────────────────────

async def main(log_path: str):
    lock = asyncio.Lock()

    if not os.path.exists(log_path):
        open(log_path, "a").close()

    last_size = os.path.getsize(log_path)

    print(f"[relay] watching {log_path}")
    print(f"[relay] routing to: {', '.join(AGENTS)}")
    print(f"[relay] Claude handled by your Claude Code session")
    print(f"[relay] poll={POLL_SEC}s  timeout={TIMEOUT_SEC}s")
    print()

    while True:
        await asyncio.sleep(POLL_SEC)

        try:
            cur_size = os.path.getsize(log_path)
        except OSError:
            continue
        if cur_size <= last_size:
            continue

        with open(log_path, "r") as f:
            content = f.read()
        last_size = cur_size

        speaker = last_speaker(content)
        if not speaker:
            continue
        # Don't re-route our own agents' replies
        if speaker in AGENTS:
            continue

        context = content[-CONTEXT_LEN:]
        targets = [a for a in AGENTS]
        print(f"[relay] [{speaker}] spoke → {', '.join(targets)}", flush=True)

        # Adaptive jitter before responding
        jitter = activity_jitter(content)
        await asyncio.sleep(jitter)

        # Re-check: if someone responded during the jitter, skip
        with open(log_path, "r") as f:
            fresh = f.read()
        if last_speaker(fresh) in AGENTS:
            last_size = os.path.getsize(log_path)
            continue

        # Acquire speaker lock
        if not acquire_lock("relay"):
            print(f"[relay] lock held by another speaker, skipping", flush=True)
            last_size = os.path.getsize(log_path)
            continue

        prompts = {
            name: PROMPT_TEMPLATE.format(name=name.capitalize(), context=context)
            for name in targets
        }
        try:
            await asyncio.gather(
                *[route_to(name, prompts[name], log_path, lock) for name in targets],
                return_exceptions=True,
            )
        finally:
            release_lock()

        # Re-read size after writes so we don't re-trigger on our own appends
        last_size = os.path.getsize(log_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="clcodgemmix relay")
    ap.add_argument("--log", default=DEFAULT_LOG, help="Path to shared log file")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.log))
    except KeyboardInterrupt:
        print("\n[relay] stopped")
