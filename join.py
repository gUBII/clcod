#!/usr/bin/env python3
"""
join.py — Join the clcodgemmix shared chat room from your terminal.

Usage:
    python3 join.py --name Farhan
    python3 join.py --name Farhan --log ./clcodgemmix.txt
"""

import asyncio
import argparse
import fcntl
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(SCRIPT_DIR, "clcodgemmix.txt")

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
    if stripped.startswith("[") and "]" in stripped:
        tag = stripped[1:stripped.index("]")]
        c = COLOURS.get(tag, "\033[97m")
        return f"{c}{BOLD}{stripped}{RESET}"
    return f"{DIM}{stripped}{RESET}" if stripped else ""


def append_message(log_path: str, name: str, text: str):
    entry = f"\n[{name.upper()}]\n{text.strip()}\n"
    with open(log_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(entry)
        fcntl.flock(f, fcntl.LOCK_UN)


async def tail_log(log_path: str):
    """Print new lines from the log as they appear."""
    with open(log_path, "r") as f:
        # Show last 30 lines on join
        content = f.read()
        recent = content.strip().split("\n")[-30:]
        for line in recent:
            coloured = colour_line(line)
            if coloured:
                print(coloured)
        print(f"\n{DIM}--- live stream ---{RESET}\n")

        # Now tail
        while True:
            line = f.readline()
            if line:
                coloured = colour_line(line.rstrip("\n"))
                if coloured:
                    sys.stdout.write(f"\r\033[K{coloured}\n")
                    sys.stdout.flush()
            else:
                await asyncio.sleep(0.3)


async def read_input(log_path: str, name: str):
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
            append_message(log_path, name, text)


async def main(name: str, log_path: str):
    if not os.path.exists(log_path):
        open(log_path, "a").close()

    print(f"\n{BOLD}clcodgemmix — shared room{RESET}")
    print(f"{DIM}You are: {name.upper()}.  Type to speak.  /quit to leave.{RESET}")

    tail_task  = asyncio.create_task(tail_log(log_path))
    input_task = asyncio.create_task(read_input(log_path, name))

    done, pending = await asyncio.wait(
        [tail_task, input_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Join the clcodgemmix shared room")
    ap.add_argument("--name", required=True, help="Your display name")
    ap.add_argument("--log",  default=DEFAULT_LOG, help="Path to shared log")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.name, args.log))
    except KeyboardInterrupt:
        print(f"\n{DIM}Disconnected.{RESET}")
