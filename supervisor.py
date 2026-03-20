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
import socket
import subprocess
import sys
import queue
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import relay
import dispatcher as dispatcher_mod

SCRIPT_DIR = Path(__file__).resolve().parent
WEB_DIR = SCRIPT_DIR / "web"


class ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_ui_url(config: dict[str, Any]) -> str:
    ui = config["ui"]
    return f"http://{ui['host']}:{ui['port']}"


USAGE_WINDOW_SECONDS = 5 * 60 * 60  # 5-hour rolling window
DEFAULT_USAGE_LIMIT = 50000         # default token budget per window


def build_usage_window(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "window_start": time.time(),
        "tokens_used": 0,
        "limit": agent.get("usage_limit", DEFAULT_USAGE_LIMIT),
    }


def build_agent_state(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": "starting",
        "session_id": None,
        "mirror_mode": agent["mirror_mode"],
        "mirror_view": "log",
        "pane_target": None,
        "pane_command": None,
        "last_error": None,
        "last_reply_at": None,
        "selected_model": agent.get("selected_model", "default"),
        "selected_effort": agent.get("selected_effort", "default"),
        "model_options": agent.get("model_options", []),
        "effort_options": agent.get("effort_options", []),
        "effort_matrix": agent.get("effort_matrix", {}),
        "usage_window": build_usage_window(agent),
    }


def build_initial_state(config: dict[str, Any]) -> dict[str, Any]:
    session = config["tmux"]["session"]
    agents = {
        agent["name"]: build_agent_state(agent)
        for agent in config["agents"]
        if agent["enabled"]
    }
    return {
        "app": {
            "phase": "booting",
            "sleeping": False,
            "ui_url": build_ui_url(config),
            "default_sender": config["ui"]["default_sender"],
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
        "project": {
            "active": None,
            "name": None,
            "path": str(SCRIPT_DIR),
        },
        "dispatcher": {
            "state": "disabled",
            "router_model": None,
            "routes_total": 0,
            "absorbs_total": 0,
            "tokens_saved": 0,
        },
        "tasks": {
            "total": 0,
            "pending": 0,
            "in_progress": 0,
            "done": 0,
            "last_created_at": None,
        },
        "transcript": {
            "path": str(config["workspace"]["log_path"]),
            "last_speaker": "",
            "last_updated_at": None,
            "rev": 0,
        },
    }


def parse_transcript_entries(text: str, limit: int) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    tagged_speaker: str | None = None
    tagged_lines: list[str] = []

    def flush_tagged() -> None:
        nonlocal tagged_speaker, tagged_lines
        if tagged_speaker and tagged_lines:
            entry = {"speaker": tagged_speaker, "text": "\n".join(tagged_lines).strip()}
            if entry["text"]:
                entries.append(entry)
        tagged_speaker = None
        tagged_lines = []

    for line in text.splitlines():
        raw_line = line.rstrip()
        line = raw_line.strip()
        if not line:
            flush_tagged()
            continue
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            flush_tagged()
            tagged_speaker = line[1:-1].strip()
            continue
        if tagged_speaker:
            tagged_lines.append(raw_line)
            continue
        try:
            payload = json.loads(line)
            if "speaker" in payload and "text" in payload:
                entry = {"speaker": payload["speaker"], "text": payload["text"]}
                if payload.get("ts"):
                    entry["ts"] = payload["ts"]
                if payload.get("seq"):
                    entry["seq"] = payload["seq"]
                entries.append(entry)
            elif "sender" in payload and "body" in payload:
                entry = {"speaker": payload["sender"], "text": payload["body"]}
                if payload.get("ts"):
                    entry["ts"] = payload["ts"]
                if payload.get("seq"):
                    entry["seq"] = payload["seq"]
                entries.append(entry)
        except json.JSONDecodeError:
            continue
    flush_tagged()
    return entries[-limit:]


def build_log_mirror_command(agent: dict[str, Any]) -> str:
    log_path = agent["io_log_path"]
    work_dir = agent.get("work_dir") or str(SCRIPT_DIR)
    script = (
        f"mkdir -p {shlex.quote(str(log_path.parent))} && "
        f"touch {shlex.quote(str(log_path))} && "
        f"printf '%s\\n\\n' {shlex.quote(f'[{agent['name']}] live log mirror')} && "
        f"exec tail -n 120 -F {shlex.quote(str(log_path))}"
    )
    return f"cd {shlex.quote(work_dir)} && bash -lc {shlex.quote(script)}"


def build_resume_mirror_command(agent: dict[str, Any], session_id: str) -> str:
    work_dir = agent.get("work_dir") or str(SCRIPT_DIR)
    args = [
        item.format_map({"session_id": session_id, "work_dir": work_dir, "script_dir": work_dir})
        for item in agent.get("mirror_resume_args", [])
    ]
    cmd = [agent["cmd"], *relay.build_selection_args(agent), *args]
    return "cd {} && exec {}".format(
        shlex.quote(work_dir),
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

    def record_agent_usage(self, name: str, tokens: int) -> None:
        """Increment token usage for an agent, resetting the window if expired."""
        with self._lock:
            agent = self.state["agents"].get(name)
            if not agent:
                return
            window = agent.setdefault("usage_window", {
                "window_start": time.time(),
                "tokens_used": 0,
                "limit": DEFAULT_USAGE_LIMIT,
            })
            now = time.time()
            if now - window["window_start"] >= USAGE_WINDOW_SECONDS:
                window["window_start"] = now
                window["tokens_used"] = 0
            window["tokens_used"] += tokens
            relay.write_json(self.path, self.state)

    def fuel_for_agent(self, name: str) -> dict[str, Any]:
        """Return fuel gauge data for a single agent."""
        with self._lock:
            agent = self.state["agents"].get(name, {})
            window = agent.get("usage_window", {})
            window_start = window.get("window_start", time.time())
            tokens_used = window.get("tokens_used", 0)
            limit = window.get("limit", DEFAULT_USAGE_LIMIT)
            now = time.time()
            # Auto-reset expired windows
            if now - window_start >= USAGE_WINDOW_SECONDS:
                tokens_used = 0
                window_start = now
            remaining = max(0, limit - tokens_used)
            pct = round((remaining / limit) * 100, 1) if limit > 0 else 0
            return {
                "window_start": window_start,
                "tokens_used": tokens_used,
                "limit": limit,
                "remaining": remaining,
                "pct_remaining": pct,
            }


class RuntimeSupervisor:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.session = config["tmux"]["session"]
        self.workspace = config["workspace"]
        self.settings_lock = threading.Lock()
        self.apply_saved_preferences()
        self.state = StateStore(config)
        self._sleeping = False
        self._sleep_lock = threading.Lock()
        self.stop_event = asyncio.Event()
        self.auth_tokens: set[str] = set()
        self._load_auth_tokens()
        self.http_server: ThreadingHTTPServer | None = None
        self.http_thread: threading.Thread | None = None
        self.mirror_keys: dict[str, tuple[str, str | None]] = {}
        self.pane_targets: dict[str, str] = {}
        self.projects_path: Path = config["workspace"]["projects_path"]
        self._sse_clients: list[queue.Queue] = []
        self._sse_lock = threading.Lock()

    @property
    def _auth_tokens_path(self) -> Path:
        return self.workspace["log_path"].parent / "auth_tokens.json"

    def _load_auth_tokens(self) -> None:
        data = relay.read_json(self._auth_tokens_path, [])
        if isinstance(data, list):
            self.auth_tokens = set(data)

    def _save_auth_tokens(self) -> None:
        relay.write_json(self._auth_tokens_path, list(self.auth_tokens))

    def sse_subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._sse_lock:
            self._sse_clients.append(q)
        return q

    def sse_unsubscribe(self, q: queue.Queue) -> None:
        with self._sse_lock:
            try:
                self._sse_clients.remove(q)
            except ValueError:
                pass

    def sse_broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        payload = json.dumps({"type": event_type, **data})
        with self._sse_lock:
            dead: list[queue.Queue] = []
            for q in self._sse_clients:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self._sse_clients.remove(q)
                except ValueError:
                    pass

    def refresh_task_state(self) -> None:
        tasks_data = relay.load_tasks(self.workspace["tasks_path"])
        all_tasks = tasks_data.get("tasks", [])
        total = len(all_tasks)
        pending = sum(1 for task in all_tasks if task.get("status") == "pending")
        in_progress = sum(
            1 for task in all_tasks if task.get("status") in ("assigned", "in_progress")
        )
        done = sum(1 for task in all_tasks if task.get("status") == "done")
        last_created_at = all_tasks[-1].get("created_at") if all_tasks else None
        self.state.patch(
            "tasks",
            {
                "total": total,
                "pending": pending,
                "in_progress": in_progress,
                "done": done,
                "last_created_at": last_created_at,
            },
        )

    def is_sleeping(self) -> bool:
        with self._sleep_lock:
            return self._sleeping

    def set_sleeping(self, value: bool) -> None:
        with self._sleep_lock:
            self._sleeping = value
        self.state.patch("app", {"sleeping": value})

    def password(self) -> str:
        env_name = self.config["ui"]["password_env"]
        return os.environ.get(env_name) or self.config["ui"]["password"]

    def preferences_payload(self) -> dict[str, Any]:
        data = relay.read_json(self.workspace["preferences_path"], {"agents": {}})
        if not isinstance(data, dict):
            return {"agents": {}}
        agents = data.get("agents", {})
        if not isinstance(agents, dict):
            agents = {}
        return {"agents": agents}

    def save_preferences_payload(self, payload: dict[str, Any]) -> None:
        relay.write_json(self.workspace["preferences_path"], payload)

    def apply_saved_preferences(self) -> None:
        preferences = self.preferences_payload()
        agent_preferences = preferences.get("agents", {})
        for agent in self.config["agents"]:
            if not agent["enabled"]:
                continue
            saved = agent_preferences.get(agent["name"], {})
            if not isinstance(saved, dict):
                saved = {}
            agent["selected_model"] = relay.resolve_selected_option(
                saved.get("selected_model", agent.get("selected_model", "default")),
                agent.get("model_options", []),
                str(agent.get("selected_model", "default")),
            )
            if agent.get("effort_options"):
                selected_effort = relay.resolve_selected_option(
                    saved.get("selected_effort", agent.get("selected_effort", "default")),
                    agent["effort_options"],
                    str(agent.get("selected_effort", "default")),
                )
                allowed_efforts = set(self.allowed_efforts_for(agent, agent["selected_model"]))
                if allowed_efforts and selected_effort not in allowed_efforts:
                    selected_effort = "default" if "default" in allowed_efforts else sorted(allowed_efforts)[0]
                agent["selected_effort"] = selected_effort
            else:
                agent["selected_effort"] = "default"

    def find_agent(self, name: str) -> dict[str, Any] | None:
        target = name.strip().upper()
        for agent in self.config["agents"]:
            if agent["name"] == target and agent["enabled"]:
                return agent
        return None

    def persist_agent_preferences(self, agent: dict[str, Any]) -> None:
        preferences = self.preferences_payload()
        preferences.setdefault("agents", {})
        preferences["agents"][agent["name"]] = {
            "selected_model": agent.get("selected_model", "default"),
            "selected_effort": agent.get("selected_effort", "default"),
        }
        self.save_preferences_payload(preferences)

    def allowed_efforts_for(self, agent: dict[str, Any], selected_model: str) -> list[str]:
        matrix = agent.get("effort_matrix", {})
        if isinstance(matrix, dict):
            allowed = matrix.get(selected_model) or matrix.get("default")
            if isinstance(allowed, list) and allowed:
                return [str(item) for item in allowed]
        return [str(item["id"]) for item in agent.get("effort_options", [])]

    def restart_agent(self, name: str) -> dict[str, Any]:
        """Kill the process in an agent's pane and respawn its mirror command."""
        agent = self.find_agent(name)
        if not agent:
            raise KeyError(name)
        pane_target = self.pane_targets.get(agent["name"])
        if not pane_target:
            raise RuntimeError(f"no pane target for {name}")
        # Force-respawn the mirror (kills whatever is running in the pane)
        self.mirror_keys.pop(agent["name"], None)
        self.sync_agent_mirrors(force=True)
        return self.state.snapshot()["agents"][agent["name"]]

    # ── Project management ──────────────────────────────────────────

    def list_projects(self) -> dict[str, Any]:
        return relay.load_projects(self.projects_path)

    def lock_project(self, path: str | None = None, url: str | None = None, name: str | None = None) -> dict[str, Any]:
        """Lock agents to a local path or clone a repo and lock to it."""
        projects = relay.load_projects(self.projects_path)
        if url:
            target_name = name or url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            target_dir = SCRIPT_DIR / "projects" / target_name
            if not target_dir.exists():
                target_dir.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["git", "clone", url, str(target_dir)],
                    check=True, timeout=120, capture_output=True, text=True,
                )
            project_path = str(target_dir.resolve())
            project_type = "cloned"
        elif path:
            resolved = Path(path).expanduser().resolve()
            if not resolved.is_dir():
                raise ValueError(f"path does not exist or is not a directory: {path}")
            project_path = str(resolved)
            project_type = "local"
            target_name = name or resolved.name
        else:
            raise ValueError("either path or url is required")

        project_id = target_name.lower().replace(" ", "-")
        projects["projects"][project_id] = {
            "name": target_name,
            "path": project_path,
            "type": project_type,
            "locked_at": utc_now(),
            "agents_allowed": [a["name"] for a in self.config["agents"] if a["enabled"]],
        }
        projects["active"] = project_id
        relay.save_projects(self.projects_path, projects)

        # Inject work_dir into runtime agent configs
        for agent in self.config["agents"]:
            if agent["enabled"]:
                agent["work_dir"] = project_path
        self.mirror_keys.clear()
        self.sync_agent_mirrors(force=True)

        # Notify transcript
        lock_body = f"[PROJECT] Locked to: {target_name} ({project_path})"
        relay.append_tagged_entry(self.workspace["log_path"], "SYSTEM", lock_body)
        return projects

    def unlock_project(self) -> dict[str, Any]:
        """Release current project lock, agents return to home repo."""
        projects = relay.load_projects(self.projects_path)
        projects["active"] = None
        relay.save_projects(self.projects_path, projects)

        for agent in self.config["agents"]:
            agent.pop("work_dir", None)
        self.mirror_keys.clear()
        self.sync_agent_mirrors(force=True)

        relay.append_tagged_entry(
            self.workspace["log_path"], "SYSTEM",
            f"[PROJECT] Unlocked — agents returned to {SCRIPT_DIR}",
        )
        return projects

    def delete_project(self, project_id: str) -> dict[str, Any]:
        projects = relay.load_projects(self.projects_path)
        if project_id not in projects["projects"]:
            raise KeyError(project_id)
        if projects["active"] == project_id:
            projects["active"] = None
            for agent in self.config["agents"]:
                agent.pop("work_dir", None)
        del projects["projects"][project_id]
        relay.save_projects(self.projects_path, projects)
        return projects

    def reset_agent_session(self, name: str) -> None:
        sessions = relay.load_sessions(self.workspace["sessions_path"])
        if name in sessions:
            del sessions[name]
            relay.save_sessions(self.workspace["sessions_path"], sessions)
        self.mirror_keys.pop(name, None)

    def update_agent_settings(
        self,
        name: str,
        selected_model: str | None,
        selected_effort: str | None,
    ) -> dict[str, Any]:
        agent = self.find_agent(name)
        if not agent:
            raise KeyError(name)

        with self.settings_lock:
            next_model = relay.resolve_selected_option(
                selected_model or agent.get("selected_model", "default"),
                agent.get("model_options", []),
                str(agent.get("selected_model", "default")),
            )
            if agent.get("effort_options"):
                next_effort = relay.resolve_selected_option(
                    selected_effort or agent.get("selected_effort", "default"),
                    agent["effort_options"],
                    str(agent.get("selected_effort", "default")),
                )
                allowed_efforts = set(self.allowed_efforts_for(agent, next_model))
                if allowed_efforts and next_effort not in allowed_efforts:
                    next_effort = "default" if "default" in allowed_efforts else sorted(allowed_efforts)[0]
            else:
                next_effort = "default"

            changed = (
                next_model != agent.get("selected_model")
                or next_effort != agent.get("selected_effort")
            )
            agent["selected_model"] = next_model
            agent["selected_effort"] = next_effort
            self.persist_agent_preferences(agent)
            if changed:
                self.reset_agent_session(agent["name"])

        self.state.patch_agent(
            agent["name"],
            {
                "selected_model": agent["selected_model"],
                "selected_effort": agent["selected_effort"],
                "session_id": None if changed else self.state.snapshot()["agents"][agent["name"]].get("session_id"),
                "mirror_view": "log" if changed else self.state.snapshot()["agents"][agent["name"]].get("mirror_view", "log"),
                "model_options": agent.get("model_options", []),
                "effort_options": agent.get("effort_options", []),
                "effort_matrix": agent.get("effort_matrix", {}),
            },
        )
        if changed:
            self.sync_agent_mirrors(force=True)
        return self.state.snapshot()["agents"][agent["name"]]

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
            self.workspace["preferences_path"],
            self.workspace["projects_path"],
            self.workspace["tasks_path"],
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace["log_path"].touch(exist_ok=True)
        if not self.workspace["sessions_path"].exists():
            relay.save_sessions(self.workspace["sessions_path"], {})
        if not self.workspace["preferences_path"].exists():
            self.save_preferences_payload({"agents": {}})
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
                        "selected_model": agent.get("selected_model", "default"),
                        "selected_effort": agent.get("selected_effort", "default"),
                        "model_options": agent.get("model_options", []),
                        "effort_options": agent.get("effort_options", []),
                        "effort_matrix": agent.get("effort_matrix", {}),
                    },
                )

        self.tmux("select-layout", "-t", f"{self.session}:0", "main-horizontal", check=False)
        self.tmux("set-window-option", "-t", f"{self.session}:0", "main-pane-height", "30", check=False)
        self.tmux("select-pane", "-t", f"{self.session}:0.0", check=False)
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
                    "selected_model": agent.get("selected_model", "default"),
                    "selected_effort": agent.get("selected_effort", "default"),
                    "model_options": agent.get("model_options", []),
                    "effort_options": agent.get("effort_options", []),
                    "effort_matrix": agent.get("effort_matrix", {}),
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

    def _send_to_socket(self, message: dict[str, Any]) -> bool:
        """Send a message to the relay socket. Returns True if sent successfully."""
        socket_path = self.workspace.get("socket_path")
        if not socket_path:
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(socket_path))
                sock.sendall(json.dumps(message).encode("utf-8"))
                return True
            finally:
                sock.close()
        except Exception as e:
            print(f"[supervisor] socket error: {e}", file=sys.stderr)
            return False

    def _pick_compact_agent(self) -> str:
        """Pick the agent with the most remaining fuel capacity. Tie-break: alphabetical."""
        best_name = ""
        best_remaining = -1
        for name in sorted(self.state.snapshot()["agents"]):
            fuel = self.state.fuel_for_agent(name)
            if fuel["remaining"] > best_remaining:
                best_remaining = fuel["remaining"]
                best_name = name
        return best_name

    def compact_context(self) -> dict[str, Any]:
        """Declutter temp logs and summarize context using local Ollama or cloud fallback."""
        cleared: list[str] = []

        # Truncate agent io logs and relay log (not the transcript itself)
        for path in [self.workspace["relay_log_path"]] + [
            a["io_log_path"] for a in self.config["agents"] if a.get("io_log_path")
        ]:
            try:
                relay.write_text(Path(path), "")
                cleared.append(str(path))
            except OSError:
                pass

        # Delete any .tmp files under the runtime dir
        runtime_dir = self.workspace["log_path"].parent
        for tmp in runtime_dir.glob("**/*.tmp"):
            tmp.unlink(missing_ok=True)
            cleared.append(str(tmp))

        dispatcher_config = self.config.get("dispatcher", {})
        use_local = dispatcher_config.get("enabled", False)
        summary = ""
        chosen_agent = "LOCAL"

        if use_local:
            # Try local Ollama summarization first
            try:
                context = relay.read_text(self.workspace["log_path"])
                summary = asyncio.run(dispatcher_mod.summarize_context(context[-6000:], dispatcher_config))
            except Exception:
                summary = ""

        if summary:
            relay.append_tagged_entry(
                self.workspace["log_path"], "SYSTEM",
                f"[COMPACT SUMMARY] {summary}",
            )
            injected = True
        else:
            # Fall back to cloud agent summarization
            chosen_agent = self._pick_compact_agent()
            summary_request = (
                f"[COMPACT → {chosen_agent}] Please read the full conversation above and reply with a "
                "single paragraph that summarises all key decisions, features built, open "
                "questions, and next actions. Keep it under 120 words. This will replace the "
                "working context for all agents."
            )
            # Write directly to transcript — skip socket dispatch to avoid
            # creating a task and broadcasting to all agents (BUG-8).
            relay.append_tagged_entry(self.workspace["log_path"], "SYSTEM", summary_request)
            injected = True

        return {"cleared": cleared, "injected": injected, "chosen_agent": chosen_agent}

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
            self.sse_broadcast("relay_state", {"state": event["state"]})
            return

        if event["type"] == "transcript":
            current_rev = self.state.state.get("transcript", {}).get("rev", 0)
            self.state.patch(
                "transcript",
                {
                    "last_speaker": event.get("last_speaker", ""),
                    "last_updated_at": event.get("last_updated_at"),
                    "rev": current_rev + 1,
                },
            )
            # Track usage: use character count as token proxy (~4 chars ≈ 1 token)
            speaker = event.get("last_speaker", "").strip().upper()
            char_count = event.get("char_count", 0)
            if speaker and speaker in self.state.snapshot()["agents"] and char_count > 0:
                estimated_tokens = max(1, char_count // 4)
                self.state.record_agent_usage(speaker, estimated_tokens)
            msg = event.get("message")
            self.sse_broadcast("transcript", {"last_speaker": event.get("last_speaker", ""), "rev": current_rev + 1, **({"message": msg} if msg else {})})
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
                values["last_activity_ts"] = event["last_reply_at"]
            if "tokens_delta" in event:
                values["tokens_delta"] = event["tokens_delta"]
                # Accumulate reply tokens into the agent's fuel gauge
                tokens = event["tokens_delta"]
                if tokens > 0:
                    self.state.record_agent_usage(event["agent"], tokens)

            # ── Pressure metrics ──
            agent_name = event.get("agent", "")
            now = time.time()
            snapshot = self.state.snapshot()
            agent_snap = snapshot.get("agents", {}).get(agent_name, {})
            pressure = agent_snap.get("pressure", {
                "queue_depth": 0,
                "last_latency_ms": 0,
                "tokens_per_sec": 0,
                "error_rate_5m": 0,
                "dispatch_ts": 0,
            })
            if event["state"] == "warming":
                pressure["dispatch_ts"] = now
                pressure["queue_depth"] = pressure.get("queue_depth", 0) + 1
            elif event["state"] == "ready":
                dispatch_ts = pressure.get("dispatch_ts", 0)
                if dispatch_ts > 0:
                    latency_ms = int((now - dispatch_ts) * 1000)
                    pressure["last_latency_ms"] = latency_ms
                    # Rolling average tokens/sec from latency + token delta
                    td = event.get("tokens_delta", 0)
                    elapsed = max(0.1, now - dispatch_ts)
                    if td > 0:
                        new_tps = td / elapsed
                        old_tps = pressure.get("tokens_per_sec", 0)
                        pressure["tokens_per_sec"] = round(old_tps * 0.6 + new_tps * 0.4, 1)
                pressure["queue_depth"] = max(0, pressure.get("queue_depth", 0) - 1)
                pressure["dispatch_ts"] = 0
            elif event["state"] == "error":
                pressure["queue_depth"] = max(0, pressure.get("queue_depth", 0) - 1)
                # Track errors in a simple counter (decay handled in JS)
                pressure["error_rate_5m"] = pressure.get("error_rate_5m", 0) + 1
            values["pressure"] = pressure

            self.state.patch_agent(event["agent"], values)
            self.sse_broadcast("agent_state", {
                "agent": agent_name,
                "state": event["state"],
                "last_error": event.get("last_error"),
            })
            return

        if event["type"] == "dispatcher":
            action = event.get("action", "route")
            snapshot = self.state.snapshot()
            dispatcher_state = snapshot.get("dispatcher", {})
            routes = dispatcher_state.get("routes_total", 0)
            absorbs = dispatcher_state.get("absorbs_total", 0)
            tokens_saved = dispatcher_state.get("tokens_saved", 0)
            if action == "absorb":
                absorbs += 1
                tokens_saved += 500  # estimated tokens saved per absorbed message
            elif action == "route":
                routes += 1
                # Calculate tokens saved by not broadcasting to all agents
                all_agents = len([a for a in self.config["agents"] if a["enabled"]])
                routed = len(event.get("targets", []))
                if routed < all_agents:
                    tokens_saved += (all_agents - routed) * 200
            self.state.patch("dispatcher", {
                "state": "active",
                "routes_total": routes,
                "absorbs_total": absorbs,
                "tokens_saved": tokens_saved,
                "last_action": action,
                "last_targets": event.get("targets", []),
            })
            self.sse_broadcast("dispatcher", {
                "action": action,
                "routes_total": routes,
                "absorbs_total": absorbs,
                "tokens_saved": tokens_saved,
            })
            return

        if event["type"] == "task_created":
            self.refresh_task_state()
            self.sse_broadcast("task_created", event.get("task", {}))

        if event["type"] == "task_updated":
            self.refresh_task_state()
            self.sse_broadcast("task_updated", {"task": event.get("task", {})})

        if event["type"] == "tasks_updated":
            self.refresh_task_state()
            self.sse_broadcast("tasks_updated", {
                "tasks": event.get("tasks", []),
                "new_status": event.get("new_status", ""),
            })

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

            def _payload(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                parsed = json.loads(raw or "{}")
                return parsed if isinstance(parsed, dict) else {}

            def _file(self, path: Path, content_type: str) -> None:
                if not path.exists():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                body = path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
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
                    snapshot = supervisor.state.snapshot()
                    # Enrich each agent with fuel gauge data
                    for agent_name in snapshot.get("agents", {}):
                        snapshot["agents"][agent_name]["fuel"] = supervisor.state.fuel_for_agent(agent_name)
                    return self._json(snapshot)
                if parsed.path == "/api/transcript":
                    if not self._authorized():
                        return self._json({"error": "locked"}, status=HTTPStatus.UNAUTHORIZED)
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["120"])[0])
                    transcript = relay.read_text(supervisor.workspace["log_path"])
                    current_rev = supervisor.state.state.get("transcript", {}).get("rev", 0)
                    return self._json({"entries": parse_transcript_entries(transcript, max(1, min(limit, 500))), "rev": current_rev})
                if parsed.path == "/api/projects":
                    if not self._authorized():
                        return self._json({"error": "locked"}, status=HTTPStatus.UNAUTHORIZED)
                    return self._json(supervisor.list_projects())
                if parsed.path == "/api/tasks":
                    if not self._authorized():
                        return self._json({"error": "locked"}, status=HTTPStatus.UNAUTHORIZED)
                    query = parse_qs(parsed.query)
                    status_filter = query.get("status", [None])[0]
                    tasks_data = relay.load_tasks(supervisor.workspace["tasks_path"])
                    tasks_list = tasks_data.get("tasks", [])
                    if status_filter:
                        tasks_list = [t for t in tasks_list if t.get("status") == status_filter]
                    return self._json({"tasks": tasks_list[-100:]})
                if parsed.path == "/api/dispatcher/health":
                    if not self._authorized():
                        return self._json({"error": "locked"}, status=HTTPStatus.UNAUTHORIZED)
                    dispatcher_config = supervisor.config.get("dispatcher", {})
                    host = dispatcher_config.get("ollama_host", "http://localhost:11434")
                    try:
                        health = asyncio.run(dispatcher_mod.health_check(host))
                    except Exception:
                        health = {"available": False, "models": []}
                    snapshot = supervisor.state.snapshot()
                    d = snapshot.get("dispatcher", {})
                    return self._json({
                        "ok": True,
                        "available": health.get("available", False),
                        "models": health.get("models", []),
                        "router_model": dispatcher_config.get("router_model"),
                        "state": d.get("state", "disabled"),
                        "routes_total": d.get("routes_total", 0),
                        "absorbs_total": d.get("absorbs_total", 0),
                        "tokens_saved": d.get("tokens_saved", 0),
                        "last_action": d.get("last_action"),
                        "last_targets": d.get("last_targets", []),
                    })
                if parsed.path.startswith("/api/agents/") and parsed.path.endswith("/logs"):
                    parts = [p for p in parsed.path.split("/") if p]
                    if len(parts) != 4:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    if not self._authorized():
                        return self._json({"error": "locked"}, status=HTTPStatus.UNAUTHORIZED)
                    agent_name = parts[2]
                    query = parse_qs(parsed.query)
                    tail = min(int(query.get("tail", ["30"])[0]), 200)
                    agent = supervisor.find_agent(agent_name)
                    if not agent:
                        return self._json({"ok": False, "error": "unknown agent"}, status=HTTPStatus.NOT_FOUND)
                    io_log_path = agent.get("io_log_path")
                    lines: list[str] = []
                    if io_log_path and Path(io_log_path).exists():
                        text = relay.read_text(Path(io_log_path))
                        lines = text.splitlines()[-tail:]
                    snapshot = supervisor.state.snapshot()
                    agent_state = snapshot.get("agents", {}).get(agent["name"], {})
                    return self._json({
                        "ok": True,
                        "agent": agent["name"],
                        "lines": lines,
                        "state": agent_state,
                    })
                if parsed.path == "/api/events":
                    if not self._authorized():
                        self.send_error(HTTPStatus.UNAUTHORIZED)
                        return
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    q = supervisor.sse_subscribe()
                    try:
                        # Send initial full state as first event
                        snapshot = supervisor.state.snapshot()
                        for agent_name in snapshot.get("agents", {}):
                            snapshot["agents"][agent_name]["fuel"] = supervisor.state.fuel_for_agent(agent_name)
                        init_data = json.dumps({"type": "init", **snapshot})
                        self.wfile.write(f"data: {init_data}\n\n".encode())
                        self.wfile.flush()
                        while True:
                            try:
                                payload = q.get(timeout=15)
                                self.wfile.write(f"data: {payload}\n\n".encode())
                                self.wfile.flush()
                            except queue.Empty:
                                # Send keepalive comment
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        supervisor.sse_unsubscribe(q)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/api/unlock":
                    payload = self._payload()
                    if str(payload.get("password", "")) != supervisor.password():
                        return self._json({"ok": False, "error": "invalid password"}, status=HTTPStatus.UNAUTHORIZED)

                    token = secrets.token_urlsafe(24)
                    supervisor.auth_tokens.add(token)
                    supervisor._save_auth_tokens()
                    headers = {
                        "Set-Cookie": f"clcod_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800"
                    }
                    return self._json({"ok": True, "state": supervisor.state.snapshot()}, headers=headers)

                if not self._authorized():
                    return self._json({"error": "locked"}, status=HTTPStatus.UNAUTHORIZED)

                if parsed.path == "/api/chat":
                    payload = self._payload()
                    raw_name = str(payload.get("name") or supervisor.config["ui"]["default_sender"]).strip()
                    raw_message = str(payload.get("message") or "").strip()
                    if not raw_name:
                        return self._json({"ok": False, "error": "name is required"}, status=HTTPStatus.BAD_REQUEST)
                    if not raw_message:
                        return self._json({"ok": False, "error": "message is required"}, status=HTTPStatus.BAD_REQUEST)
                    
                    speaker = raw_name[:40]
                    message = {
                        "id": secrets.token_urlsafe(16),
                        "sender": speaker,
                        "seq": int(time.time() * 1000),
                        "type": "message",
                        "body": raw_message[:8000],
                        "ts": utc_now(),
                    }
                    
                    if not supervisor._send_to_socket(message):
                        relay.append_tagged_entry(supervisor.workspace["log_path"], speaker, raw_message[:8000])

                    # Bump transcript revision on every message to signal clients to refresh
                    current_rev = supervisor.state.state.get("transcript", {}).get("rev", 0)
                    supervisor.state.patch(
                        "transcript",
                        {
                            "last_speaker": speaker,
                            "last_updated_at": utc_now(),
                            "rev": current_rev + 1,
                        },
                    )
                    supervisor.sse_broadcast("transcript", {
                        "last_speaker": speaker,
                        "rev": current_rev + 1,
                        "message": message,
                    })
                    return self._json({"ok": True, "state": supervisor.state.snapshot()})

                if parsed.path.startswith("/api/agents/") and parsed.path.endswith("/settings"):
                    parts = [part for part in parsed.path.split("/") if part]
                    if len(parts) != 4:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    agent_name = parts[2]
                    payload = self._payload()
                    try:
                        agent_state = supervisor.update_agent_settings(
                            agent_name,
                            str(payload.get("selected_model") or "").strip() or None,
                            str(payload.get("selected_effort") or "").strip() or None,
                        )
                    except KeyError:
                        return self._json({"ok": False, "error": "unknown agent"}, status=HTTPStatus.NOT_FOUND)
                    return self._json({"ok": True, "agent": agent_state, "state": supervisor.state.snapshot()})

                if parsed.path.startswith("/api/agents/") and parsed.path.endswith("/restart"):
                    parts = [part for part in parsed.path.split("/") if part]
                    if len(parts) != 4:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    agent_name = parts[2]
                    try:
                        agent_state = supervisor.restart_agent(agent_name)
                    except KeyError:
                        return self._json({"ok": False, "error": "unknown agent"}, status=HTTPStatus.NOT_FOUND)
                    except RuntimeError as exc:
                        return self._json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return self._json({"ok": True, "agent": agent_state, "state": supervisor.state.snapshot()})

                if parsed.path == "/api/compact":
                    result = supervisor.compact_context()
                    return self._json({
                        "ok": True,
                        "cleared": result["cleared"],
                        "injected": result["injected"],
                        "chosen_agent": result.get("chosen_agent", ""),
                    })

                if parsed.path == "/api/repo/pull":
                    try:
                        git_proc = subprocess.run(
                            ["git", "pull"],
                            cwd=str(SCRIPT_DIR),
                            text=True,
                            capture_output=True,
                            timeout=30,
                        )
                        chmod_proc = subprocess.run(
                            ["chmod", "-R", "u+rw,g+rw", "."],
                            cwd=str(SCRIPT_DIR),
                            text=True,
                            capture_output=True,
                            timeout=15,
                        )
                        ok = git_proc.returncode == 0
                        # Write sync message directly to transcript (BUG-8: skip socket to avoid task/dispatch)
                        sync_body = f"[SYNC] Repository synced. All agents now share read-write access to {SCRIPT_DIR}."
                        relay.append_tagged_entry(supervisor.workspace["log_path"], "SYSTEM", sync_body)
                        return self._json({
                            "ok": ok,
                            "stdout": git_proc.stdout,
                            "stderr": git_proc.stderr,
                            "chmod_ok": chmod_proc.returncode == 0,
                            "chmod_stderr": chmod_proc.stderr,
                            "sync_path": str(SCRIPT_DIR),
                        })
                    except Exception as exc:
                        return self._json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

                if parsed.path == "/api/sleep":
                    payload = self._payload()
                    want_sleep = bool(payload.get("sleep", not supervisor.is_sleeping()))
                    supervisor.set_sleeping(want_sleep)
                    if not want_sleep:
                        # Write wake message directly to transcript (BUG-8: skip socket to avoid task/dispatch)
                        wake_body = (
                            "[WAKE] All agents are back online. Review the messages above that "
                            "arrived during sleep and continue where we left off."
                        )
                        relay.append_tagged_entry(
                            supervisor.workspace["log_path"], "SYSTEM", wake_body
                        )
                    return self._json({
                        "ok": True,
                        "sleeping": want_sleep,
                        "state": supervisor.state.snapshot(),
                    })

                # ── Project endpoints ──────────────────────────────
                if parsed.path == "/api/projects/lock":
                    payload = self._payload()
                    try:
                        projects = supervisor.lock_project(
                            path=str(payload.get("path") or "").strip() or None,
                            url=str(payload.get("url") or "").strip() or None,
                            name=str(payload.get("name") or "").strip() or None,
                        )
                        supervisor.state.patch("project", {
                            "active": projects["active"],
                            "name": projects["projects"].get(projects["active"], {}).get("name"),
                            "path": projects["projects"].get(projects["active"], {}).get("path", str(SCRIPT_DIR)),
                        })
                        return self._json({"ok": True, "projects": projects})
                    except (ValueError, subprocess.CalledProcessError) as exc:
                        return self._json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

                if parsed.path == "/api/projects/unlock":
                    projects = supervisor.unlock_project()
                    supervisor.state.patch("project", {
                        "active": None,
                        "name": None,
                        "path": str(SCRIPT_DIR),
                    })
                    return self._json({"ok": True, "projects": projects})

                # ── Task endpoints ────────────────────────────────
                if parsed.path == "/api/tasks":
                    payload = self._payload()
                    title = str(payload.get("title") or "").strip()
                    if not title:
                        return self._json({"ok": False, "error": "title is required"}, status=HTTPStatus.BAD_REQUEST)
                    task = relay.create_task(
                        supervisor.workspace["tasks_path"],
                        title=title,
                        task_type=str(payload.get("type") or "general"),
                        priority=str(payload.get("priority") or "normal"),
                        assigned_to=payload.get("assigned_to") if isinstance(payload.get("assigned_to"), list) else None,
                        source_message=str(payload.get("source_message") or ""),
                    )
                    supervisor.refresh_task_state()
                    return self._json({"ok": True, "task": task})

                if parsed.path.startswith("/api/tasks/") and not parsed.path.endswith("/"):
                    parts = [p for p in parsed.path.split("/") if p]
                    if len(parts) == 3:
                        try:
                            task_id = int(parts[2])
                        except ValueError:
                            return self._json({"ok": False, "error": "invalid task id"}, status=HTTPStatus.BAD_REQUEST)
                        payload = self._payload()
                        tasks_data = relay.load_tasks(supervisor.workspace["tasks_path"])
                        task = next((t for t in tasks_data["tasks"] if t["id"] == task_id), None)
                        if not task:
                            return self._json({"ok": False, "error": "task not found"}, status=HTTPStatus.NOT_FOUND)
                        if "status" in payload and payload["status"] in relay.TASK_STATUSES:
                            task["status"] = payload["status"]
                            if payload["status"] == "done":
                                task["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        if "assigned_to" in payload and isinstance(payload["assigned_to"], list):
                            task["assigned_to"] = payload["assigned_to"]
                        if "priority" in payload:
                            task["priority"] = str(payload["priority"])
                        relay.save_tasks(supervisor.workspace["tasks_path"], tasks_data)
                        supervisor.refresh_task_state()
                        supervisor.sse_broadcast("task_updated", {"task": task})
                        return self._json({"ok": True, "task": task})

                self.send_error(HTTPStatus.NOT_FOUND)

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
        tick = 0
        while not self.stop_event.is_set():
            self.refresh_transcript_state()
            self.refresh_tmux_state()
            tick += 1
            if tick % 3 == 0 and self._sse_clients:
                snapshot = self.state.snapshot()
                for agent_name in snapshot.get("agents", {}):
                    snapshot["agents"][agent_name]["fuel"] = self.state.fuel_for_agent(agent_name)
                self.sse_broadcast("state_refresh", snapshot)
            await asyncio.sleep(1.0)

    async def run(self) -> int:
        self.prepare_runtime()
        self.start_http_server()
        self.ensure_tmux_layout()

        # Check dispatcher (Ollama) health on startup
        dispatcher_config = self.config.get("dispatcher", {})
        if dispatcher_config.get("enabled"):
            health = await dispatcher_mod.health_check(dispatcher_config.get("ollama_host", "http://localhost:11434"))
            if health["available"]:
                self.state.patch("dispatcher", {
                    "state": "active",
                    "router_model": dispatcher_config.get("router_model"),
                    "models_loaded": health["models"],
                })
                print(f"[supervisor] dispatcher active — Ollama models: {health['models']}", flush=True)
            else:
                self.state.patch("dispatcher", {"state": "unavailable"})
                print("[supervisor] dispatcher unavailable — Ollama not reachable, falling back to broadcast", flush=True)

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
                is_sleeping=self.is_sleeping,
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
