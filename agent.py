#!/usr/bin/env python3
"""
Shared-room agent. Run one process per AI.

Usage:
  ANTHROPIC_API_KEY=sk-ant-...  python3 agent.py claude
  OPENAI_API_KEY=sk-...         python3 agent.py codex
  GOOGLE_API_KEY=AI...          python3 agent.py gemini
"""

import os, sys, time, random, re, textwrap

# ── config ────────────────────────────────────────────────────────────────────
SPEAKER      = sys.argv[1].upper()          # CLAUDE | CODEX | GEMINI
LOG_PATH     = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "claude_codex_log.txt")
POLL_SEC     = 1.0    # how often to check for new content
JITTER_MAX   = 2.5    # max random wait before replying (avoids 3-way collision)
MAX_TOKENS   = 300    # keep replies tight
SYSTEM_PROMPT = (
    "You are {name}, one of three AI agents (Claude, Codex, Gemini) sharing a "
    "real-time text log. You speak authentically as yourself. Keep replies "
    "concise (2-6 sentences). React to what was just said. You may address "
    "the others by name. Do NOT prefix your reply with your own name — the "
    "log wrapper does that."
).format(name=SPEAKER.capitalize())

# ── log helpers ───────────────────────────────────────────────────────────────
def read_log() -> str:
    with open(LOG_PATH, "r") as f:
        return f.read()

def log_size() -> int:
    return os.path.getsize(LOG_PATH)

def last_speaker(text: str) -> str:
    """Return the last [SPEAKER] tag found, or ''."""
    tags = re.findall(r"\[([A-Z]+)\]", text)
    return tags[-1] if tags else ""

def append_reply(text: str):
    entry = f"\n[{SPEAKER}]\n{text.strip()}\n"
    with open(LOG_PATH, "a") as f:
        f.write(entry)
    print(entry, flush=True)

# ── API calls ─────────────────────────────────────────────────────────────────
def call_claude(context: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )
    return msg.content[0].text

def call_codex(context: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": context},
        ],
    )
    return resp.choices[0].message.content

def call_gemini(context: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
    model = genai.GenerativeModel(
        model_name="gemini-1.5-pro",
        system_instruction=SYSTEM_PROMPT,
    )
    resp = model.generate_content(context)
    return resp.text

CALLERS = {
    "CLAUDE": call_claude,
    "CODEX":  call_codex,
    "GEMINI": call_gemini,
}

# ── main loop ─────────────────────────────────────────────────────────────────
def main():
    print(f"[agent.py] {SPEAKER} online — watching {LOG_PATH}", flush=True)
    seen_size = log_size()

    while True:
        time.sleep(POLL_SEC)
        current_size = log_size()
        if current_size <= seen_size:
            continue

        content  = read_log()
        seen_size = current_size
        who_last  = last_speaker(content)

        if who_last == SPEAKER:
            continue  # we just spoke, don't echo ourselves

        # jitter so all three don't reply simultaneously
        jitter = random.uniform(0.3, JITTER_MAX)
        time.sleep(jitter)

        # after jitter, check nobody else (including us) already replied
        content2  = read_log()
        seen_size = log_size()
        if last_speaker(content2) == SPEAKER:
            continue  # we sneaked in already somehow

        try:
            reply = CALLERS[SPEAKER](content2[-6000:])  # send last ~6k chars
            append_reply(reply)
            seen_size = log_size()
        except Exception as e:
            print(f"[agent.py] ERROR: {e}", flush=True)

if __name__ == "__main__":
    main()
