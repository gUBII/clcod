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
    if stripped.startswith("[") and "]" in stripped:
        tag = stripped[1:stripped.index("]")]
        c = COLOURS.get(tag, "\033[97m")
        return f"{c}{BOLD}{stripped}{RESET}"
    return f"{DIM}{stripped}{RESET}" if stripped else ""


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


def append_message(log_path: Path, name: str, text: str) -> None:
    entry = f"\n[{name.upper()}]\n{text.strip()}\n"
    with log_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(entry)
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


async def read_input(log_path: Path, name: str) -> None:
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


async def main(name: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    print(f"\n{BOLD}clcodgemmix — shared room{RESET}")
    print(f"{DIM}You are: {name.upper()}. Type to speak. /quit to leave.{RESET}")
    print(f"{DIM}Log: {log_path}{RESET}")

    tail_task = asyncio.create_task(tail_log(log_path))
    input_task = asyncio.create_task(read_input(log_path, name))

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
        asyncio.run(main(args.name, resolve_log_path(args.config, args.log)))
    except KeyboardInterrupt:
        print(f"\n{DIM}Disconnected.{RESET}")
