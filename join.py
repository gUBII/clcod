#!/usr/bin/env python3
"""
join.py - Join the clcodgemmix shared chat room from your terminal.

Usage:
    python3 join.py --name Farhan
    python3 join.py --name Farhan --config ./config.json
    python3 join.py --name Farhan --log ./clcodgemmix.txt
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"
DEFAULT_LOG = SCRIPT_DIR / "clcodgemmix.txt"

# ANSI colours per speaker
COLOURS = {
    "CLAUDE":  "\033[96m",   # cyan
    "CODEX":   "\033[92m",   # green
    "GEMINI":  "\033[93m",   # yellow
    "FARHAN":  "\033[95m",   # magenta
}
RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"


def colour_line(line: str) -> str:
    stripped = line.rstrip()
    if not stripped:
        return ""
    try:
        payload = json.loads(stripped)
        if "sender" in payload and "body" in payload:
            tag = payload["sender"].upper()
            c = COLOURS.get(tag, "\033[97m")
            return f"{c}{BOLD}[{tag}]{RESET}\n{payload['body']}"
    except json.JSONDecodeError:
        pass
        
    # fallback for raw text lines
    if stripped.startswith("[") and "]" in stripped:
        tag = stripped[1:stripped.index("]")]
        c = COLOURS.get(tag, "\033[97m")
        return f"{c}{BOLD}{stripped}{RESET}"
    return f"{DIM}{stripped}{RESET}"

def resolve_socket_path(config_path: str | Path) -> Path:
    config_file = Path(config_path).expanduser()
    if not config_file.is_absolute():
        config_file = (Path.cwd() / config_file).resolve()

    if config_file.exists():
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
        else:
            workspace = config.get("workspace", {})
            raw_socket_path = workspace.get("socket_path")
            if raw_socket_path:
                path = Path(str(raw_socket_path)).expanduser()
                if not path.is_absolute():
                    path = (config_file.parent / path).resolve()
                return path

    return SCRIPT_DIR / ".clcod-runtime/room.sock"

def resolve_log_path(config_path: str | Path, explicit_log: str | None) -> Path:
    if explicit_log:
        path = Path(explicit_log).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path

    config_file = Path(config_path).expanduser()
    if not config_file.is_absolute():
        config_file = (Path.cwd() / config_file).resolve()

    if config_file.exists():
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid config JSON: {config_file}: {exc}") from exc
        workspace = config.get("workspace", {})
        raw_log_path = workspace.get("log_path")
        if raw_log_path:
            path = Path(str(raw_log_path)).expanduser()
            if not path.is_absolute():
                path = (config_file.parent / path).resolve()
            return path

    return DEFAULT_LOG

import time
import uuid
import socket

def append_message(socket_path: Path, name: str, text: str) -> None:
    message = {
        "id": str(uuid.uuid4()),
        "sender": name.upper(),
        "seq": int(time.time() * 1000),
        "type": "message",
        "body": text.strip(),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload = json.dumps(message).encode("utf-8")
    
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(socket_path))
        sock.sendall(payload)
    except Exception as e:
        print(f"\n{DIM}Failed to send message: {e}{RESET}")
    finally:
        sock.close()


async def tail_log(log_path: Path) -> None:
    """Print new lines from the log as they appear."""
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        # Show last 30 lines on join
        content = handle.read()
        recent = content.strip().split("\n")[-30:]
        for line in recent:
            coloured = colour_line(line)
            if coloured:
                print(coloured)
        print(f"\n{DIM}--- live stream ---{RESET}\n")

        # Now tail
        while True:
            line = handle.readline()
            if line:
                coloured = colour_line(line.rstrip("\n"))
                if coloured:
                    sys.stdout.write(f"\r\033[K{coloured}\n")
                    sys.stdout.flush()
            else:
                await asyncio.sleep(0.3)


async def read_input(socket_path: Path, name: str) -> None:
    """Read user input and append to log."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except EOFError:
            break
        if not line:
            break
        text = line.strip()
        if text.lower() in ("/quit", "/exit", "quit", "exit"):
            print(f"{DIM}Leaving the room.{RESET}")
            break
        if text:
            append_message(socket_path, name, text)


async def main(name: str, config_path: str, explicit_log: str | None) -> None:
    log_path = resolve_log_path(config_path, explicit_log)
    socket_path = resolve_socket_path(config_path)
    
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    print(f"\n{BOLD}clcodgemmix — shared room{RESET}")
    print(f"{DIM}You are: {name.upper()}. Type to speak. /quit to leave.{RESET}")
    print(f"{DIM}Log: {log_path}{RESET}")
    print(f"{DIM}Socket: {socket_path}{RESET}")

    tail_task = asyncio.create_task(tail_log(log_path))
    input_task = asyncio.create_task(read_input(socket_path, name))

    done, pending = await asyncio.wait(
        [tail_task, input_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Join the clcodgemmix shared room")
    parser.add_argument("--name", required=True, help="Your display name")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    parser.add_argument("--log", help="Path to shared log")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.name, args.config, args.log))
    except KeyboardInterrupt:
        print(f"\n{DIM}Disconnected.{RESET}")
