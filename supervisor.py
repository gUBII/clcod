#!/usr/bin/env python3
"""
supervisor.py - Own the relay lifecycle, tmux mirrors, runtime state, and
local web UI for clcod.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import secrets
import shlex
import signal
import subprocess
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import relay

SCRIPT_DIR = Path(__file__).resolve().parent
WEB_DIR = SCRIPT_DIR / "web"


class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_ui_url(config: dict[str, Any]) -> str:
    ui = config["ui"]
    return f"http://{ui['host']}:{ui['port']}"


def build_initial_state(config: dict[str, Any]) -> dict[str, Any]:
    session = config["tmux"]["session"]
    agents = {
        agent["name"]: {
            "state": "starting",
            "session_id": None,
            "mirror_mode": agent["mirror_mode"],
            "mirror_view": "log",
            "pane_target": None,
            "pane_command": None,
            "last_error": None,
            "last_reply_at": None,
        }
        for agent in config["agents"]
        if agent["enabled"]
    }
    return {
        "app": {
            "phase": "booting",
            "ui_url": build_ui_url(config),
        },
        "relay": {
            "state": "starting",
            "pid": os.getpid(),
            "last_error": None,
        },
        "tmux": {
            "session": session,
            "state": "starting",
            "attach_command": f"tmux attach -t {session}",
        },
        "agents": agents,
        "transcript": {
            "path": str(config["workspace"]["log_path"]),
            "last_speaker": "",
            "last_updated_at": None,
        },
    }


def parse_transcript_entries(text: str, limit: int) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    speaker: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            if speaker is not None:
                entries.append({"speaker": speaker, "text": "\n".join(body).strip()})
            speaker = line[1:-1].strip()
            body = []
            continue
        if speaker is not None:
            body.append(line)
    if speaker is not None:
        entries.append({"speaker": speaker, "text": "\n".join(body).strip()})
    return entries[-limit:]


def build_log_mirror_command(agent: dict[str, Any]) -> str:
    log_path = agent["io_log_path"]
    script = (
        f"mkdir -p {shlex.quote(str(log_path.parent))} && "
        f"touch {shlex.quote(str(log_path))} && "
        f"printf '%s\\n\\n' {shlex.quote(f'[{agent['name']}] live log mirror')} && "
        f"exec tail -n 120 -F {shlex.quote(str(log_path))}"
    )
    return f"cd {shlex.quote(str(SCRIPT_DIR))} && bash -lc {shlex.quote(script)}"


def build_resume_mirror_command(agent: dict[str, Any], session_id: str) -> str:
    args = [
        item.format_map({"session_id": session_id})
        for item in agent.get("mirror_resume_args", [])
    ]
    cmd = [agent["cmd"], *args]
    return "cd {} && exec {}".format(
        shlex.quote(str(SCRIPT_DIR)),
        " ".join(shlex.quote(part) for part in cmd),
    )


def desired_mirror_view(agent: dict[str, Any], session_id: str | None) -> str:
    if agent["mirror_mode"] == "resume" and session_id and agent.get("mirror_resume_args"):
        return "resume"
    return "log"


def infer_agent_state(
    current_state: str,
    relay_state: str,
    mirror_view: str,
    pane_command: str | None,
) -> str:
    if current_state == "error":
        return "error"
    if not pane_command:
        return "starting"
    if relay_state != "running":
        return "auth"
    if mirror_view in {"resume", "log"}:
        return "ready"
    return "warming"


class StateStore:
    def __init__(self, config: dict[str, Any]) -> None:
        self.path: Path = config["workspace"]["state_path"]
        self._lock = threading.Lock()
        self.state = build_initial_state(config)
        self.write()

    def write(self) -> None:
        with self._lock:
            relay.write_json(self.path, self.state)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.state)

    def patch(self, section: str, values: dict[str, Any]) -> None:
        with self._lock:
            self.state[section].update(values)
            relay.write_json(self.path, self.state)

    def patch_agent(self, name: str, values: dict[str, Any]) -> None:
        with self._lock:
            self.state["agents"][name].update(values)
            relay.write_json(self.path, self.state)


class RuntimeSupervisor:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.session = config["tmux"]["session"]
        self.workspace = config["workspace"]
        self.state = StateStore(config)
        self.stop_event = asyncio.Event()
        self.auth_tokens: set[str] = set()
        self.http_server: ThreadingHTTPServer | None = None
        self.http_thread: threading.Thread | None = None
        self.mirror_keys: dict[str, tuple[str, str | None]] = {}
        self.pane_targets: dict[str, str] = {}

    def password(self) -> str:
        env_name = self.config["ui"]["password_env"]
        return os.environ.get(env_name) or self.config["ui"]["password"]

    def tmux(self, *args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", *args],
            cwd=str(SCRIPT_DIR),
            text=True,
            capture_output=capture,
            check=check,
        )

    def tmux_session_exists(self) -> bool:
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session],
            cwd=str(SCRIPT_DIR),
            text=True,
            capture_output=True,
        )
        return result.returncode == 0

    def prepare_runtime(self) -> None:
        for path in (
            self.workspace["log_path"],
            self.workspace["relay_log_path"],
            self.workspace["state_path"],
            self.workspace["sessions_path"],
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace["log_path"].touch(exist_ok=True)
        if not self.workspace["sessions_path"].exists():
            relay.save_sessions(self.workspace["sessions_path"], {})
        for agent in self.config["agents"]:
            if agent["enabled"]:
                agent["io_log_path"].parent.mkdir(parents=True, exist_ok=True)
                agent["io_log_path"].touch(exist_ok=True)
        relay.write_text(self.workspace["pid_path"], f"{os.getpid()}\n")

    def ensure_tmux_layout(self) -> None:
        if self.tmux_session_exists():
            self.tmux("kill-session", "-t", self.session, check=False)

        self.tmux("new-session", "-d", "-s", self.session, "-x", "220", "-y", "60")
        self.tmux("rename-window", "-t", f"{self.session}:0", "engines")
        self.tmux("set-window-option", "-t", f"{self.session}:0", "remain-on-exit", "on")
        self.tmux(
            "respawn-pane",
            "-k",
            "-t",
            f"{self.session}:0.0",
            f"cd {shlex.quote(str(SCRIPT_DIR))} && exec bash watch-log.sh --config {shlex.quote(str(self.config['config_path']))}",
        )
        agent_root = self.tmux(
            "split-window",
            "-v",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            f"{self.session}:0.0",
            "-p",
            "35",
            capture=True,
        ).stdout.strip()

        enabled_agents = [agent for agent in self.config["agents"] if agent["enabled"]]
        if enabled_agents:
            current_pane = agent_root
            for idx, agent in enumerate(enabled_agents):
                if idx == 0:
                    pane_target = current_pane
                else:
                    pane_target = self.tmux(
                        "split-window",
                        "-h",
                        "-P",
                        "-F",
                        "#{pane_id}",
                        "-t",
                        current_pane,
                        capture=True,
                    ).stdout.strip()
                    current_pane = pane_target
                self.pane_targets[agent["name"]] = pane_target
                self.state.patch_agent(
                    agent["name"],
                    {
                        "pane_target": pane_target,
                        "mirror_mode": agent["mirror_mode"],
                        "mirror_view": "log",
                    },
                )

        self.tmux("select-layout", "-t", f"{self.session}:0", "main-horizontal", check=False)
        self.tmux(
            "new-window",
            "-d",
            "-t",
            self.session,
            "-n",
            "runtime",
            f"cd {shlex.quote(str(SCRIPT_DIR))} && touch {shlex.quote(str(self.workspace['relay_log_path']))} && exec tail -n 120 -F {shlex.quote(str(self.workspace['relay_log_path']))}",
        )
        self.state.patch("tmux", {"state": "running"})
        self.sync_agent_mirrors(force=True)

    def sync_agent_mirrors(self, force: bool = False) -> None:
        sessions = relay.load_sessions(self.workspace["sessions_path"])
        for agent in [item for item in self.config["agents"] if item["enabled"]]:
            name = agent["name"]
            pane_target = self.pane_targets.get(name)
            if not pane_target:
                continue
            session_id = sessions.get(name)
            mirror_view = desired_mirror_view(agent, session_id)
            mirror_key = (mirror_view, session_id)
            if force or self.mirror_keys.get(name) != mirror_key:
                if mirror_view == "resume" and session_id:
                    cmd = build_resume_mirror_command(agent, session_id)
                else:
                    cmd = build_log_mirror_command(agent)
                self.tmux("respawn-pane", "-k", "-t", pane_target, cmd)
                self.mirror_keys[name] = mirror_key
            self.state.patch_agent(
                name,
                {
                    "session_id": session_id,
                    "mirror_view": mirror_view,
                    "pane_target": pane_target,
                    "mirror_mode": agent["mirror_mode"],
                },
            )

    def collect_pane_commands(self) -> dict[str, str]:
        if not self.tmux_session_exists():
            return {}
        result = self.tmux(
            "list-panes",
            "-t",
            f"{self.session}:0",
            "-F",
            "#{pane_id}\t#{pane_current_command}",
            capture=True,
        )
        pane_commands: dict[str, str] = {}
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            pane_id, pane_command = line.split("\t", 1)
            pane_commands[pane_id] = pane_command.strip()
        return pane_commands

    def refresh_transcript_state(self) -> None:
        log_path = self.workspace["log_path"]
        text = relay.read_text(log_path) if log_path.exists() else ""
        self.state.patch(
            "transcript",
            {
                "last_speaker": relay.last_speaker(text),
                "last_updated_at": utc_now() if text else None,
            },
        )

    def refresh_tmux_state(self) -> None:
        if not self.tmux_session_exists():
            self.state.patch("tmux", {"state": "missing"})
            self.state.patch("app", {"phase": "error"})
            return

        self.state.patch("tmux", {"state": "running"})
        self.sync_agent_mirrors(force=False)
        pane_commands = self.collect_pane_commands()
        snapshot = self.state.snapshot()
        relay_state = snapshot["relay"]["state"]
        for name, agent_state in snapshot["agents"].items():
            pane_target = agent_state.get("pane_target")
            pane_command = pane_commands.get(pane_target or "")
            inferred_state = infer_agent_state(
                current_state=agent_state["state"],
                relay_state=relay_state,
                mirror_view=agent_state.get("mirror_view", "log"),
                pane_command=pane_command,
            )
            values = {"pane_command": pane_command, "state": inferred_state}
            if pane_command is None and agent_state["state"] != "error":
                values["state"] = "starting"
            self.state.patch_agent(name, values)

        app_phase = "booting"
        if snapshot["relay"]["state"] == "error":
            app_phase = "error"
        elif snapshot["relay"]["state"] == "running" and self.tmux_session_exists():
            app_phase = "ready"
        self.state.patch("app", {"phase": app_phase})

    def handle_relay_event(self, event: dict[str, Any]) -> None:
        if event["type"] == "relay_state":
            self.state.patch(
                "relay",
                {
                    "state": event["state"],
                    "pid": os.getpid(),
                    "last_error": event.get("last_error"),
                },
            )
            return

        if event["type"] == "transcript":
            self.state.patch(
                "transcript",
                {
                    "last_speaker": event.get("last_speaker", ""),
                    "last_updated_at": event.get("last_updated_at"),
                },
            )
            return

        if event["type"] == "agent_state":
            values = {
                "state": event["state"],
                "last_error": event.get("last_error"),
            }
            if "session_id" in event:
                values["session_id"] = event["session_id"]
            if "last_reply_at" in event:
                values["last_reply_at"] = event["last_reply_at"]
            self.state.patch_agent(event["agent"], values)

    def make_handler(self) -> type[BaseHTTPRequestHandler]:
        supervisor = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _cookie_token(self) -> str | None:
                raw = self.headers.get("Cookie")
                if not raw:
                    return None
                cookie = SimpleCookie()
                cookie.load(raw)
                morsel = cookie.get("clcod_session")
                return morsel.value if morsel else None

            def _authorized(self) -> bool:
                token = self._cookie_token()
                return bool(token and token in supervisor.auth_tokens)

            def _json(self, payload: Any, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if headers:
                    for key, value in headers.items():
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _file(self, path: Path, content_type: str) -> None:
                if not path.exists():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                body = path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    return self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
                if parsed.path == "/app.js":
                    return self._file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
                if parsed.path == "/styles.css":
                    return self._file(WEB_DIR / "styles.css", "text/css; charset=utf-8")
                if parsed.path == "/api/state":
                    if not self._authorized():
                        return self._json({"locked": True, "app": {"phase": "locked"}})
                    return self._json(supervisor.state.snapshot())
                if parsed.path == "/api/transcript":
                    if not self._authorized():
                        return self._json({"error": "locked"}, status=HTTPStatus.UNAUTHORIZED)
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["120"])[0])
                    transcript = relay.read_text(supervisor.workspace["log_path"])
                    return self._json({"entries": parse_transcript_entries(transcript, max(1, min(limit, 500)))})
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != "/api/unlock":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                if str(payload.get("password", "")) != supervisor.password():
                    return self._json({"ok": False, "error": "invalid password"}, status=HTTPStatus.UNAUTHORIZED)

                token = secrets.token_urlsafe(24)
                supervisor.auth_tokens.add(token)
                headers = {
                    "Set-Cookie": f"clcod_session={token}; Path=/; HttpOnly; SameSite=Strict"
                }
                return self._json({"ok": True, "state": supervisor.state.snapshot()}, headers=headers)

        return Handler

    def start_http_server(self) -> None:
        handler = self.make_handler()
        self.http_server = ReusableHTTPServer(
            (self.config["ui"]["host"], self.config["ui"]["port"]),
            handler,
        )
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()

    async def refresh_loop(self) -> None:
        while not self.stop_event.is_set():
            self.refresh_transcript_state()
            self.refresh_tmux_state()
            await asyncio.sleep(1.0)

    async def run(self) -> int:
        self.prepare_runtime()
        self.start_http_server()
        self.ensure_tmux_layout()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass

        refresh_task = asyncio.create_task(self.refresh_loop())
        relay_task = asyncio.create_task(
            relay.run_relay(
                self.config,
                event_callback=self.handle_relay_event,
                stop_event=self.stop_event,
            )
        )

        result = 0
        try:
            result = await relay_task
        except Exception as exc:
            self.state.patch("relay", {"state": "error", "last_error": str(exc)})
            self.state.patch("app", {"phase": "error"})
            raise
        finally:
            self.stop_event.set()
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            if self.http_server is not None:
                self.http_server.shutdown()
                self.http_server.server_close()
        return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="clcod supervisor")
    parser.add_argument("--config", default=str(relay.DEFAULT_CONFIG_PATH), help="Path to config.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = relay.load_config(args.config)
    supervisor = RuntimeSupervisor(config)
    return asyncio.run(supervisor.run())


if __name__ == "__main__":
    raise SystemExit(main())
