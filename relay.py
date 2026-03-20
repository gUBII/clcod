#!/usr/bin/env python3
"""
relay.py - Watches the shared transcript and routes new human messages to the
configured agent CLIs.

Usage:
    python3 relay.py
    python3 relay.py --config ./config.json
    python3 relay.py --log ./clcodgemmix.txt
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import fcntl
import json
import os
import re
import shlex
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import dispatcher as dispatcher_mod

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "agents": [
        {
            "name": "CLAUDE",
            "enabled": True,
            "cmd": "claude",
            "args": ["-p", "--dangerously-skip-permissions"],
            "invoke_resume_args": ["-p", "--dangerously-skip-permissions", "--session-id", "{session_id}"],
            "mirror_resume_args": ["--resume", "{session_id}"],
            "model_arg": ["--model", "{value}"],
            "effort_arg": ["--effort", "{value}"],
            "model_options": ["default", "sonnet", "opus"],
            "effort_options": ["default", "low", "medium", "high", "max"],
            "mirror_mode": "resume",
            "preseed_session_id": True,
            "timeout": 180,
        },
        {
            "name": "CODEX",
            "enabled": True,
            "cmd": "codex",
            "args": [
                "exec",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                "{script_dir}",
            ],
            "invoke_resume_args": ["exec", "resume", "{session_id}"],
            "mirror_resume_args": ["resume", "--no-alt-screen", "-C", "{script_dir}", "{session_id}"],
            "model_arg": ["-m", "{value}"],
            "effort_arg": ["-c", "model_reasoning_effort=\"{value}\""],
            "mirror_mode": "resume",
            "preseed_session_id": False,
            "selected_effort": "medium",
            "timeout": 60,
        },
        {
            "name": "GEMINI",
            "enabled": True,
            "cmd": "gemini",
            "args": ["-p"],
            "model_arg": ["--model", "{value}"],
            "model_options": ["default", "gemini-2.5-pro", "gemini-2.5-flash"],
            "mirror_mode": "log",
            "preseed_session_id": False,
            "timeout": 60,
        },
    ],
    "workspace": {
        "log_path": "clcodgemmix.txt",
        "lock_path": "speaker.lock",
        "poll_sec": 0.5,
        "context_len": 6000,
        "relay_log_path": ".clcod-runtime/relay.log",
        "pid_path": ".clcod-runtime/supervisor.pid",
        "state_path": ".clcod-runtime/state.json",
        "sessions_path": ".clcod-runtime/sessions.json",
        "preferences_path": ".clcod-runtime/preferences.json",
        "agent_logs_dir": ".clcod-runtime/agents",
        "projects_path": ".clcod-runtime/projects.json",
        "tasks_path": ".clcod-runtime/tasks.json",
    },
    "locks": {
        "ttl": 90,
    },
    "tmux": {
        "session": "triagent",
    },
    "ui": {
        "host": "127.0.0.1",
        "port": 4173,
        "password_env": "CLCOD_PASSWORD",
        "password": "free",
        "default_sender": "Operator",
        "open_browser": True,
    },
    "dispatcher": {
        "enabled": True,
        "ollama_host": "http://localhost:11434",
        "router_model": "qwen3.5:latest",
        "summarizer_model": "qwen3.5:9b",
        "validator_model": "rnj-1:8b",
        "router_timeout": 15,
        "summarizer_timeout": 30,
        "validator_timeout": 10,
        "fallback_action": "route",
    },
}

PROMPT_TEMPLATE = (
    "You are {name}, one of three AI agents (Claude, Codex, Gemini) sharing a "
    "real-time terminal chat room. Your working directory is {work_dir}. "
    "Below is the recent conversation log. "
    "Reply naturally as yourself in 2-5 sentences. Do not prefix your reply "
    "with your name or a [TAG].\n\n{context}"
)

SESSION_PATTERNS: dict[str, re.Pattern[str]] = {
    "CODEX": re.compile(r"session id:\s*([0-9a-f-]{36})", re.IGNORECASE),
}

EventCallback = Callable[[dict[str, Any]], None]
EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh", "max"]


class SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def relay_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[relay {timestamp}] {message}", flush=True)


def merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge_dicts(base[key], value)
        else:
            base[key] = value
    return base


def resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def interpolate(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format_map(SafeFormatDict(variables))
    if isinstance(value, list):
        return [interpolate(item, variables) for item in value]
    return value


def read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError:
        return copy.deepcopy(default)


def write_json(path: Path, payload: Any) -> None:
    write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def build_option(
    raw_value: str,
    *,
    label: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    value = raw_value.strip()
    option_value: str | None = None if value == "default" else value
    return {
        "id": value,
        "label": label or ("Default" if value == "default" else value),
        "value": option_value,
        "description": description,
    }


def dedupe_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for option in options:
        option_id = str(option["id"])
        if option_id in seen:
            continue
        seen.add(option_id)
        result.append(option)
    return result


def effort_rank(value: str) -> int:
    try:
        return EFFORT_ORDER.index(value)
    except ValueError:
        return len(EFFORT_ORDER)


def discover_codex_catalog() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    cache_path = Path.home() / ".codex" / "models_cache.json"
    payload = read_json(cache_path, {})
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return [build_option("default")], [build_option("default")], {}

    effort_ids: set[str] = set()
    effort_matrix: dict[str, list[str]] = {}
    normalized_models: list[tuple[int, dict[str, Any]]] = []

    for raw_model in models:
        if not isinstance(raw_model, dict):
            continue
        if raw_model.get("visibility") not in {None, "list"}:
            continue
        slug = str(raw_model.get("slug", "")).strip()
        if not slug:
            continue
        display_name = str(raw_model.get("display_name") or slug).strip()
        description = str(raw_model.get("description") or "").strip() or None
        priority = int(raw_model.get("priority", 9999))
        normalized_models.append(
            (
                priority,
                build_option(slug, label=display_name, description=description),
            )
        )
        levels = raw_model.get("supported_reasoning_levels", [])
        if isinstance(levels, list):
            efforts = []
            for level in levels:
                if not isinstance(level, dict):
                    continue
                effort = str(level.get("effort", "")).strip()
                if not effort:
                    continue
                effort_ids.add(effort)
                efforts.append(effort)
            if efforts:
                effort_matrix[slug] = ["default", *sorted(set(efforts), key=effort_rank)]

    model_options = [build_option("default")]
    model_options.extend(option for _, option in sorted(normalized_models, key=lambda item: (item[0], item[1]["label"])))
    effort_options = [build_option("default")]
    effort_options.extend(build_option(effort) for effort in sorted(effort_ids, key=effort_rank))
    return dedupe_options(model_options), dedupe_options(effort_options), effort_matrix


def normalize_option_list(
    raw_options: Any,
    agent_name: str,
    field_name: str,
    fallback_options: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if raw_options in (None, []):
        return copy.deepcopy(fallback_options)
    if not isinstance(raw_options, list):
        raise ValueError(f"{agent_name}: {field_name} must be a list")

    options: list[dict[str, Any]] = []
    for item in raw_options:
        if isinstance(item, str):
            options.append(build_option(item))
            continue
        if isinstance(item, dict):
            option_id = str(item.get("id") or item.get("value") or "").strip()
            if not option_id:
                raise ValueError(f"{agent_name}: {field_name} option requires id or value")
            label = item.get("label")
            description = item.get("description")
            options.append(
                build_option(
                    option_id,
                    label=str(label) if label is not None else None,
                    description=str(description) if description is not None else None,
                )
            )
            continue
        raise ValueError(f"{agent_name}: {field_name} entries must be strings or objects")

    return dedupe_options(options)


def normalize_effort_matrix(raw_matrix: Any, agent_name: str) -> dict[str, list[str]]:
    if raw_matrix in (None, {}):
        return {}
    if not isinstance(raw_matrix, dict):
        raise ValueError(f"{agent_name}: effort_matrix must be an object")
    matrix: dict[str, list[str]] = {}
    for key, value in raw_matrix.items():
        if not isinstance(value, list):
            raise ValueError(f"{agent_name}: effort_matrix values must be lists")
        matrix[str(key)] = [str(item) for item in value]
    return matrix


def default_agent_controls(name: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
    if name == "CODEX":
        return discover_codex_catalog()
    if name == "CLAUDE":
        models = [build_option("default"), build_option("sonnet"), build_option("opus")]
        efforts = [
            build_option("default"),
            build_option("low"),
            build_option("medium"),
            build_option("high"),
            build_option("max"),
        ]
        matrix = {
            "default": ["default", "low", "medium", "high", "max"],
            "sonnet": ["default", "low", "medium", "high", "max"],
            "opus": ["default", "low", "medium", "high", "max"],
        }
        return models, efforts, matrix
    if name == "GEMINI":
        models = [
            build_option("default"),
            build_option("gemini-2.5-pro"),
            build_option("gemini-2.5-flash"),
        ]
        return models, [], {}
    return [build_option("default")], [], {}


def resolve_selected_option(
    selected_id: Any,
    options: list[dict[str, Any]],
    fallback_id: str,
) -> str:
    valid_ids = {str(item["id"]) for item in options}
    normalized = str(selected_id or fallback_id or "default")
    if normalized in valid_ids:
        return normalized
    if fallback_id in valid_ids:
        return fallback_id
    if options:
        return str(options[0]["id"])
    return "default"


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path).expanduser()
    if not config_path.is_absolute():
        config_path = (Path.cwd() / config_path).resolve()

    config_dir = config_path.parent
    config = copy.deepcopy(DEFAULT_CONFIG)

    if config_path.exists():
        try:
            raw = json.loads(read_text(config_path))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid config JSON: {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"config root must be an object: {config_path}")
        merge_dicts(config, raw)

    workspace = config.get("workspace", {})
    locks = config.get("locks", {})
    tmux = config.get("tmux", {})
    ui = config.get("ui", {})
    dispatcher_cfg = config.get("dispatcher", {})
    variables = {
        "script_dir": str(SCRIPT_DIR),
        "config_dir": str(config_dir),
    }

    normalized_workspace = {
        "log_path": resolve_path(config_dir, str(workspace.get("log_path", "clcodgemmix.txt"))),
        "lock_path": resolve_path(config_dir, str(workspace.get("lock_path", "speaker.lock"))),
        "socket_path": resolve_path(config_dir, str(workspace.get("socket_path", ".clcod-runtime/room.sock"))),
        "poll_sec": float(workspace.get("poll_sec", 0.5)),
        "context_len": int(workspace.get("context_len", 6000)),
        "relay_log_path": resolve_path(
            config_dir, str(workspace.get("relay_log_path", ".clcod-runtime/relay.log"))
        ),
        "pid_path": resolve_path(
            config_dir, str(workspace.get("pid_path", ".clcod-runtime/supervisor.pid"))
        ),
        "state_path": resolve_path(
            config_dir, str(workspace.get("state_path", ".clcod-runtime/state.json"))
        ),
        "sessions_path": resolve_path(
            config_dir, str(workspace.get("sessions_path", ".clcod-runtime/sessions.json"))
        ),
        "preferences_path": resolve_path(
            config_dir, str(workspace.get("preferences_path", ".clcod-runtime/preferences.json"))
        ),
        "agent_logs_dir": resolve_path(
            config_dir, str(workspace.get("agent_logs_dir", ".clcod-runtime/agents"))
        ),
        "projects_path": resolve_path(
            config_dir, str(workspace.get("projects_path", ".clcod-runtime/projects.json"))
        ),
        "tasks_path": resolve_path(
            config_dir, str(workspace.get("tasks_path", ".clcod-runtime/tasks.json"))
        ),
    }
    variables.update(
        {
            "log_path": str(normalized_workspace["log_path"]),
            "lock_path": str(normalized_workspace["lock_path"]),
            "socket_path": str(normalized_workspace["socket_path"]),
            "relay_log_path": str(normalized_workspace["relay_log_path"]),
            "pid_path": str(normalized_workspace["pid_path"]),
            "state_path": str(normalized_workspace["state_path"]),
            "sessions_path": str(normalized_workspace["sessions_path"]),
            "preferences_path": str(normalized_workspace["preferences_path"]),
            "agent_logs_dir": str(normalized_workspace["agent_logs_dir"]),
            "projects_path": str(normalized_workspace["projects_path"]),
            "tasks_path": str(normalized_workspace["tasks_path"]),
        }
    )

    agents: list[dict[str, Any]] = []
    raw_agents = config.get("agents", [])
    if not isinstance(raw_agents, list):
        raise ValueError("config.agents must be a list")
    for raw_agent in raw_agents:
        if not isinstance(raw_agent, dict):
            raise ValueError("each agent config must be an object")

        name = str(raw_agent.get("name", "")).strip().upper()
        cmd = str(raw_agent.get("cmd", "")).strip()
        if not name or not cmd:
            raise ValueError("each agent requires non-empty name and cmd fields")

        args = normalize_argv(raw_agent.get("args", []), name, "args")
        invoke_resume_args = normalize_argv(
            raw_agent.get("invoke_resume_args", []), name, "invoke_resume_args"
        )
        mirror_resume_args = normalize_argv(
            raw_agent.get("mirror_resume_args", []), name, "mirror_resume_args"
        )
        model_arg = normalize_argv(raw_agent.get("model_arg", []), name, "model_arg")
        effort_arg = normalize_argv(raw_agent.get("effort_arg", []), name, "effort_arg")
        mirror_mode = str(raw_agent.get("mirror_mode", "log")).strip().lower()
        if mirror_mode not in {"resume", "log"}:
            raise ValueError(f"{name}: mirror_mode must be 'resume' or 'log'")

        raw_preseed_session_id = raw_agent.get("preseed_session_id", False)
        if not isinstance(raw_preseed_session_id, (bool, str)):
            raise ValueError(f"{name}: preseed_session_id must be a boolean or string")

        default_models, default_efforts, default_effort_matrix = default_agent_controls(name)
        model_options = normalize_option_list(
            raw_agent.get("model_options"),
            name,
            "model_options",
            default_models,
        )
        effort_options = normalize_option_list(
            raw_agent.get("effort_options"),
            name,
            "effort_options",
            default_efforts if effort_arg else [],
        )
        effort_matrix = normalize_effort_matrix(raw_agent.get("effort_matrix"), name) or default_effort_matrix
        selected_model = resolve_selected_option(
            raw_agent.get("selected_model", "default"),
            model_options,
            "default",
        )
        selected_effort = resolve_selected_option(
            raw_agent.get("selected_effort", "default"),
            effort_options or [build_option("default")],
            "default",
        )

        agent = {
            "name": name,
            "enabled": bool(raw_agent.get("enabled", True)),
            "cmd": interpolate(cmd, variables),
            "args": interpolate(args, variables),
            "invoke_resume_args": interpolate(invoke_resume_args, variables),
            "mirror_resume_args": interpolate(mirror_resume_args, variables),
            "model_arg": interpolate(model_arg, variables),
            "effort_arg": interpolate(effort_arg, variables),
            "model_options": interpolate(model_options, variables),
            "effort_options": interpolate(effort_options, variables),
            "effort_matrix": interpolate(effort_matrix, variables),
            "selected_model": selected_model,
            "selected_effort": selected_effort if effort_options else "default",
            "mirror_mode": mirror_mode,
            "preseed_session_id": interpolate(raw_preseed_session_id, variables),
            "timeout": int(raw_agent.get("timeout", 60)),
            "io_log_path": normalized_workspace["agent_logs_dir"] / f"{name.lower()}.log",
        }
        agents.append(agent)

    if not agents:
        raise ValueError("config must define at least one agent")

    return {
        "config_path": config_path,
        "agents": agents,
        "workspace": normalized_workspace,
        "locks": {
            "ttl": int(locks.get("ttl", 90)),
        },
        "tmux": {
            "session": str(tmux.get("session", "triagent")),
        },
        "ui": {
            "host": str(ui.get("host", "127.0.0.1")),
            "port": int(ui.get("port", 4173)),
            "password_env": str(ui.get("password_env", "CLCOD_PASSWORD")),
            "password": str(ui.get("password", "free")),
            "default_sender": str(ui.get("default_sender") or os.environ.get("USER") or "Operator"),
            "open_browser": bool(ui.get("open_browser", True)),
        },
        "dispatcher": {
            "enabled": bool(dispatcher_cfg.get("enabled", True)),
            "ollama_host": str(dispatcher_cfg.get("ollama_host", "http://localhost:11434")),
            "router_model": str(dispatcher_cfg.get("router_model", "qwen3.5:latest")),
            "summarizer_model": str(dispatcher_cfg.get("summarizer_model", "qwen3.5:9b")),
            "validator_model": str(dispatcher_cfg.get("validator_model", "rnj-1:8b")),
            "router_timeout": int(dispatcher_cfg.get("router_timeout", 15)),
            "summarizer_timeout": int(dispatcher_cfg.get("summarizer_timeout", 30)),
            "validator_timeout": int(dispatcher_cfg.get("validator_timeout", 10)),
            "fallback_action": str(dispatcher_cfg.get("fallback_action", "route")),
        },
    }


def normalize_argv(value: Any, agent_name: str, field_name: str) -> list[str]:
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ValueError(f"{agent_name}: {field_name} must be a string or list")


def last_speaker(text: str) -> str:
    last = ""
    tagged_speaker: str | None = None
    tagged_body_seen = False
    for line in text.splitlines():
        raw_line = line.rstrip()
        line = raw_line.strip()
        if not line:
            if tagged_speaker and tagged_body_seen:
                last = tagged_speaker
                tagged_speaker = None
                tagged_body_seen = False
            continue
        try:
            payload = json.loads(line)
            if "sender" in payload:
                last = payload["sender"]
            tagged_speaker = None
            tagged_body_seen = False
        except json.JSONDecodeError:
            if line.startswith("[") and line.endswith("]") and len(line) > 2:
                if tagged_speaker and tagged_body_seen:
                    last = tagged_speaker
                tagged_speaker = line[1:-1].strip()
                tagged_body_seen = False
                continue
            if tagged_speaker:
                tagged_body_seen = True
    if tagged_speaker and tagged_body_seen:
        last = tagged_speaker
    return last


def activity_jitter(content: str) -> float:
    recent = content[-500:]
    msg_count = recent.count("\n[")
    if msg_count > 4:
        return 0.5
    if msg_count > 2:
        return 1.0
    return 2.0


def acquire_lock(lock_path: Path, owner: str, ttl: int) -> bool:
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.exists():
            age = time.time() - lock_path.stat().st_mtime
            if age < ttl:
                return False
        write_text(lock_path, f"{owner}:{time.time()}\n")
        return True
    except OSError:
        return False


def release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def append_tagged_entry(log_path: Path, speaker: str, text: str) -> None:
    message = {
        "id": str(uuid.uuid4()),
        "sender": speaker,
        "seq": int(time.time() * 1000),
        "type": "message",
        "body": text.strip(),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    entry = json.dumps(message) + "\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(entry)
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def append_reply(write_lock: asyncio.Lock, log_path: Path, speaker: str, text: str) -> None:
    async with write_lock:
        append_tagged_entry(log_path, speaker, text)


def parse_codex(raw: str) -> str:
    lines = raw.strip().splitlines()
    response: list[str] = []
    capture = False
    for line in lines:
        stripped = line.strip()
        if stripped == "codex":
            capture = True
            continue
        if capture:
            if stripped.startswith("tokens used"):
                break
            response.append(line)

    text = "\n".join(response).strip()
    if text:
        return text

    skip_prefixes = (
        "OpenAI",
        "--------",
        "workdir:",
        "model:",
        "provider:",
        "approval:",
        "sandbox:",
        "reasoning",
        "session id:",
        "user",
        "mcp startup:",
        "thinking",
        "codex",
        "tokens used",
    )
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("**"):
            continue
        if any(stripped.startswith(prefix) for prefix in skip_prefixes):
            continue
        try:
            int(stripped.replace(",", ""))
            continue
        except ValueError:
            return stripped
    return ""


def parse_gemini(raw: str) -> str:
    lines = raw.strip().splitlines()
    return "\n".join(line for line in lines if "Loaded cached" not in line).strip()


def parse_claude(raw: str) -> str:
    return raw.strip()


PARSERS = {
    "CLAUDE": parse_claude,
    "CODEX": parse_codex,
    "GEMINI": parse_gemini,
}


@dataclass
class AgentCallResult:
    reply: str
    raw: str
    stderr: str
    session_id: str | None


DEFAULT_PROJECTS: dict[str, Any] = {"active": None, "projects": {}}


def load_projects(path: Path) -> dict[str, Any]:
    data = read_json(path, DEFAULT_PROJECTS)
    if not isinstance(data, dict):
        return copy.deepcopy(DEFAULT_PROJECTS)
    data.setdefault("active", None)
    data.setdefault("projects", {})
    return data


def save_projects(path: Path, projects: dict[str, Any]) -> None:
    write_json(path, projects)


# ── Task queue data model ──────────────────────────────────────

DEFAULT_TASKS: dict[str, Any] = {"tasks": [], "next_id": 1}

TASK_STATUSES = {"pending", "assigned", "in_progress", "review", "done", "blocked", "failed"}


def load_tasks(path: Path) -> dict[str, Any]:
    data = read_json(path, DEFAULT_TASKS)
    if not isinstance(data, dict):
        return copy.deepcopy(DEFAULT_TASKS)
    data.setdefault("tasks", [])
    data.setdefault("next_id", 1)
    return data


def save_tasks(path: Path, tasks: dict[str, Any]) -> None:
    write_json(path, tasks)


def create_task(
    tasks_path: Path,
    *,
    title: str,
    task_type: str = "general",
    priority: str = "normal",
    assigned_to: list[str] | None = None,
    source_message: str = "",
) -> dict[str, Any]:
    """Create a new task and persist it. Returns the created task dict."""
    store = load_tasks(tasks_path)
    task_id = store["next_id"]
    store["next_id"] = task_id + 1
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    task = {
        "id": task_id,
        "title": title[:200],
        "type": task_type,
        "status": "assigned" if assigned_to else "pending",
        "priority": priority,
        "assigned_to": assigned_to or [],
        "source_message": source_message[:500],
        "created_at": now,
        "completed_at": None,
        "tokens_spent": 0,
    }
    store["tasks"].append(task)
    save_tasks(tasks_path, store)
    return task


def load_sessions(path: Path) -> dict[str, str]:
    data = read_json(path, {})
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def save_sessions(path: Path, sessions: dict[str, str]) -> None:
    write_json(path, sessions)


def emit_event(event_callback: EventCallback | None, event: dict[str, Any]) -> None:
    if event_callback is None:
        return
    event_callback(event)


def resolve_preseed_session_id(agent: dict[str, Any]) -> str | None:
    raw_value = agent.get("preseed_session_id")
    if isinstance(raw_value, str):
        value = raw_value.strip()
        return value or None
    if raw_value:
        return str(uuid.uuid4())
    return None


def extract_session_id(agent: dict[str, Any], raw: str, stderr_text: str, session_id: str | None) -> str | None:
    pattern = SESSION_PATTERNS.get(agent["name"])
    if not pattern:
        return session_id

    for text in (stderr_text, raw):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return session_id


def option_value(options: list[dict[str, Any]], option_id: str) -> str | None:
    for option in options:
        if str(option["id"]) == option_id:
            value = option.get("value")
            return str(value) if value is not None else None
    return None


def effective_effort_id(agent: dict[str, Any]) -> str:
    selected_effort = str(agent.get("selected_effort") or "default")
    if selected_effort != "default":
        return selected_effort

    if str(agent.get("name") or "").upper() != "CODEX":
        return selected_effort

    matrix = agent.get("effort_matrix", {})
    if not isinstance(matrix, dict):
        return selected_effort

    selected_model = str(agent.get("selected_model") or "default")
    allowed = matrix.get(selected_model) or matrix.get("default") or []
    for candidate in allowed:
        normalized = str(candidate)
        if normalized != "default":
            return normalized
    return selected_effort


def build_selection_args(agent: dict[str, Any]) -> list[str]:
    args: list[str] = []
    selected_model = str(agent.get("selected_model") or "default")
    model_value = option_value(agent.get("model_options", []), selected_model)
    if model_value and agent.get("model_arg"):
        args.extend(item.format_map({"value": model_value}) for item in agent["model_arg"])

    selected_effort = effective_effort_id(agent)
    effort_value = option_value(agent.get("effort_options", []), selected_effort)
    if effort_value and agent.get("effort_arg"):
        args.extend(item.format_map({"value": effort_value}) for item in agent["effort_arg"])

    return args


def build_agent_command(
    agent: dict[str, Any], prompt: str, session_id: str | None
) -> tuple[list[str], str | None]:
    effective_session_id = session_id
    work_dir = agent.get("work_dir") or str(SCRIPT_DIR)
    variables = {"work_dir": work_dir, "script_dir": work_dir}
    args = [str(a).format_map(SafeFormatDict(variables)) for a in agent["args"]]

    if not effective_session_id:
        effective_session_id = resolve_preseed_session_id(agent)

    if effective_session_id and agent["invoke_resume_args"]:
        args = [str(item).format_map(SafeFormatDict({"session_id": effective_session_id, "work_dir": work_dir, "script_dir": work_dir})) for item in agent["invoke_resume_args"]]

    selection_args = build_selection_args(agent)
    return [agent["cmd"], *selection_args, *args, prompt], effective_session_id


def seed_sessions(path: Path, agents: list[dict[str, Any]]) -> dict[str, str]:
    sessions = load_sessions(path)
    updated = False
    for agent in agents:
        if not agent["enabled"] or sessions.get(agent["name"]):
            continue
        session_id = resolve_preseed_session_id(agent)
        if session_id:
            sessions[agent["name"]] = session_id
            updated = True
    if updated:
        save_sessions(path, sessions)
    return sessions


def log_agent_io(path: Path, cmd: list[str], raw: str, stderr_text: str, session_id: str | None) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"[{timestamp}] cmd: {' '.join(shlex.quote(part) for part in cmd)}",
    ]
    if session_id:
        lines.append(f"[{timestamp}] session_id: {session_id}")
    if stderr_text:
        lines.extend([f"[{timestamp}] --- stderr ---", stderr_text])
    if raw:
        lines.extend([f"[{timestamp}] --- stdout ---", raw.rstrip()])
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


async def _exec_agent(
    agent: dict[str, Any],
    cmd: list[str],
    env: dict[str, str],
) -> tuple[bytes, bytes, int]:
    work_dir = agent.get("work_dir") or str(SCRIPT_DIR)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=agent["timeout"])
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        raise
    return stdout, stderr, proc.returncode


_SESSION_IN_USE_RE = re.compile(r"session\s+id\s+\S+\s+is\s+already\s+in\s+use", re.IGNORECASE)


async def call_agent(
    agent: dict[str, Any],
    prompt: str,
    sessions_path: Path,
    session_lock: asyncio.Lock,
) -> AgentCallResult:
    env = dict(os.environ)
    if agent["name"] == "CLAUDE":
        env["CLAUDECODE"] = ""

    async with session_lock:
        sessions = load_sessions(sessions_path)
        current_session_id = sessions.get(agent["name"])
        cmd, current_session_id = build_agent_command(agent, prompt, current_session_id)

    stdout, stderr, returncode = await _exec_agent(agent, cmd, env)

    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if stderr_text:
        relay_log(f"{agent['name']}: stderr: {stderr_text}")

    # Retry once with a fresh session ID when the stored one is still held open
    if returncode != 0 and _SESSION_IN_USE_RE.search(stderr_text):
        relay_log(f"{agent['name']}: session in use, retrying with fresh session")
        async with session_lock:
            sessions = load_sessions(sessions_path)
            sessions.pop(agent["name"], None)
            save_sessions(sessions_path, sessions)

        cmd, current_session_id = build_agent_command(agent, prompt, None)
        log_agent_io(agent["io_log_path"], cmd, "", stderr_text, current_session_id)

        stdout, stderr, returncode = await _exec_agent(agent, cmd, env)
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if stderr_text:
            relay_log(f"{agent['name']}: stderr: {stderr_text}")

    if returncode != 0:
        raise RuntimeError(f"exit code {returncode}")

    raw = stdout.decode("utf-8", errors="replace")
    parser = PARSERS.get(agent["name"], parse_claude)
    current_session_id = extract_session_id(agent, raw, stderr_text, current_session_id)
    if current_session_id:
        async with session_lock:
            sessions = load_sessions(sessions_path)
            if sessions.get(agent["name"]) != current_session_id:
                sessions[agent["name"]] = current_session_id
                save_sessions(sessions_path, sessions)

    log_agent_io(agent["io_log_path"], cmd, raw, stderr_text, current_session_id)
    return AgentCallResult(
        reply=parser(raw),
        raw=raw,
        stderr=stderr_text,
        session_id=current_session_id,
    )


async def route_to(
    agent: dict[str, Any],
    prompt: str,
    log_path: Path,
    write_lock: asyncio.Lock,
    sessions_path: Path,
    session_lock: asyncio.Lock,
    event_callback: EventCallback | None = None,
) -> None:
    name = agent["name"]
    emit_event(
        event_callback,
        {"type": "agent_state", "agent": name, "state": "warming", "last_error": None},
    )
    try:
        result = await call_agent(agent, prompt, sessions_path, session_lock)
        if result.reply:
            await append_reply(write_lock, log_path, name, result.reply)
            relay_log(f"{name} replied ({len(result.reply)} chars)")
        else:
            relay_log(f"{name}: empty reply")
        emit_event(
            event_callback,
            {
                "type": "agent_state",
                "agent": name,
                "state": "ready",
                "session_id": result.session_id,
                "last_error": None,
                "last_reply_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tokens_delta": max(1, len(result.reply) // 4) if result.reply else 0,
            },
        )
    except FileNotFoundError:
        relay_log(f"{name}: command not found: {agent['cmd']}")
        emit_event(
            event_callback,
            {
                "type": "agent_state",
                "agent": name,
                "state": "error",
                "last_error": f"command not found: {agent['cmd']}",
            },
        )
    except asyncio.TimeoutError:
        relay_log(f"{name}: timed out after {agent['timeout']}s")
        emit_event(
            event_callback,
            {
                "type": "agent_state",
                "agent": name,
                "state": "error",
                "last_error": f"timed out after {agent['timeout']}s",
            },
        )
    except Exception as exc:
        relay_log(f"{name}: error: {exc}")
        emit_event(
            event_callback,
            {
                "type": "agent_state",
                "agent": name,
                "state": "error",
                "last_error": str(exc),
            },
        )


async def run_relay(
    config: dict[str, Any],
    event_callback: EventCallback | None = None,
    stop_event: asyncio.Event | None = None,
    is_sleeping: Callable[[], bool] | None = None,
) -> int:
    workspace = config["workspace"]
    log_path: Path = workspace["log_path"]
    lock_path: Path = workspace["lock_path"]
    sessions_path: Path = workspace["sessions_path"]
    projects_path: Path = workspace["projects_path"]
    tasks_path: Path = workspace["tasks_path"]
    poll_sec = workspace["poll_sec"]
    context_len = workspace["context_len"]
    lock_ttl = config["locks"]["ttl"]
    enabled_agents = [agent for agent in config["agents"] if agent["enabled"]]
    managed_names = {agent["name"] for agent in enabled_agents}
    write_lock = asyncio.Lock()
    session_lock = asyncio.Lock()

    if not enabled_agents:
        raise ValueError("no enabled agents configured")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    if not sessions_path.exists():
        save_sessions(sessions_path, {})
    last_size = log_path.stat().st_size

    relay_log(f"watching {log_path}")
    relay_log(f"routing to: {', '.join(agent['name'] for agent in enabled_agents)}")
    relay_log(
        "poll="
        f"{poll_sec}s context_len={context_len} lock_ttl={lock_ttl}s "
        f"timeouts={[agent['timeout'] for agent in enabled_agents]}"
    )
    emit_event(event_callback, {"type": "relay_state", "state": "running"})
    for agent in enabled_agents:
        emit_event(
            event_callback,
            {
                "type": "agent_state",
                "agent": agent["name"],
                "state": "starting",
                "mirror_mode": agent["mirror_mode"],
            },
        )

    socket_path: Path = workspace["socket_path"]
    
    # ... setup code above remains ...
    internal_stop_event = stop_event or asyncio.Event()
    if stop_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, internal_stop_event.set)
            except NotImplementedError:
                pass

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await reader.read(65536)
            if not data:
                return
            
            payload = json.loads(data.decode("utf-8"))
            sender = payload.get("sender", "UNKNOWN")
            body = payload.get("body", "")
            if not body:
                return

            append_tagged_entry(log_path, sender, body)
            
            emit_event(
                event_callback,
                {
                    "type": "transcript",
                    "last_speaker": sender,
                    "last_updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "char_count": len(body),
                },
            )
            relay_log(f"[{sender}] spoke -> {', '.join(agent['name'] for agent in enabled_agents)}")

            # Skip routing when sleeping — message is already saved to transcript
            if is_sleeping and is_sleeping():
                relay_log(f"sleeping — message from {sender} saved but not routed")
                return

            # Resolve active project work_dir
            projects = load_projects(projects_path)
            active_id = projects.get("active")
            active_project = projects.get("projects", {}).get(active_id or "") if active_id else None
            work_dir = active_project["path"] if active_project else str(SCRIPT_DIR)

            # Inject work_dir into each agent for this dispatch
            for agent in enabled_agents:
                agent["work_dir"] = work_dir

            # Read back context to ensure we get the full log state
            fresh = read_text(log_path)
            context = fresh[-context_len:]

            # ── Dispatcher: smart routing via local Ollama models ──
            dispatch_config = config.get("dispatcher", {})
            dispatch_targets = enabled_agents  # default: all agents
            if dispatch_config.get("enabled"):
                try:
                    decision = await dispatcher_mod.classify_message(body, context, dispatch_config)
                    action = decision.get("action", "route")
                    relay_log(f"dispatcher: action={action} targets={decision.get('targets')} type={decision.get('task_type')}")
                    emit_event(event_callback, {
                        "type": "dispatcher",
                        "action": action,
                        "targets": decision.get("targets", []),
                        "task_type": decision.get("task_type"),
                        "priority": decision.get("priority"),
                    })

                    if action == "absorb" and decision.get("reply"):
                        # Handle locally — no cloud call needed
                        await append_reply(write_lock, log_path, "DISPATCHER", decision["reply"])
                        relay_log(f"dispatcher absorbed: {decision['reply'][:80]}")
                        return

                    if action == "clarify" and decision.get("reply"):
                        await append_reply(write_lock, log_path, "DISPATCHER", decision["reply"])
                        relay_log(f"dispatcher clarifying: {decision['reply'][:80]}")
                        return

                    if action == "route" and decision.get("targets"):
                        target_names = set(decision["targets"])
                        filtered = [a for a in enabled_agents if a["name"] in target_names]
                        if filtered:
                            dispatch_targets = filtered
                            relay_log(f"dispatcher routed to: {', '.join(a['name'] for a in dispatch_targets)}")
                except Exception as exc:
                    relay_log(f"dispatcher error (falling back to broadcast): {exc}")

            # ── Create task for tracking ──
            task_title = body[:120] if len(body) <= 120 else body[:117] + "..."
            task_type = "general"
            task_priority = "normal"
            if dispatch_config.get("enabled"):
                try:
                    task_type = decision.get("task_type", "general")  # type: ignore[possibly-undefined]
                    task_priority = decision.get("priority", "normal")  # type: ignore[possibly-undefined]
                except NameError:
                    pass
            task = create_task(
                tasks_path,
                title=task_title,
                task_type=task_type,
                priority=task_priority,
                assigned_to=[a["name"] for a in dispatch_targets],
                source_message=body,
            )
            relay_log(f"task #{task['id']} created: {task['title'][:60]}")
            emit_event(event_callback, {
                "type": "task_created",
                "task": task,
            })

            prompts = {
                agent["name"]: PROMPT_TEMPLATE.format(
                    name=agent["name"].capitalize(),
                    context=context,
                    work_dir=work_dir,
                )
                for agent in dispatch_targets
            }

            if not acquire_lock(lock_path, f"relay:{os.getpid()}", lock_ttl):
                relay_log("speaker.lock is active; skipping this dispatch cycle")
                return

            try:
                await asyncio.gather(
                    *[
                        route_to(
                            agent,
                            prompts[agent["name"]],
                            log_path,
                            write_lock,
                            sessions_path,
                            session_lock,
                            event_callback,
                        )
                        for agent in dispatch_targets
                    ]
                )
            finally:
                release_lock(lock_path)
                
        except json.JSONDecodeError:
            relay_log("received invalid JSON on socket")
        except Exception as e:
            relay_log(f"socket error: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def batch_pending_tasks() -> None:
        """Periodically check for pending (unassigned) tasks and dispatch them."""
        while not internal_stop_event.is_set():
            try:
                await asyncio.sleep(30)
                if is_sleeping and is_sleeping():
                    continue
                store = load_tasks(tasks_path)
                pending = [t for t in store["tasks"] if t["status"] == "pending"]
                if not pending:
                    continue
                # Group pending tasks by type, dispatch as a single batch message
                batch_body_parts = []
                batch_ids = []
                for task in pending[:5]:  # process up to 5 at a time
                    batch_body_parts.append(f"- [{task['type']}] {task['title']}")
                    batch_ids.append(task["id"])
                    task["status"] = "assigned"
                    task["assigned_to"] = [a["name"] for a in enabled_agents]
                batch_body = "Batch tasks:\n" + "\n".join(batch_body_parts)
                save_tasks(tasks_path, store)
                relay_log(f"batch processing {len(batch_ids)} pending tasks: {batch_ids}")

                fresh = read_text(log_path)
                context = fresh[-context_len:]
                work_dir = str(SCRIPT_DIR)
                projects = load_projects(projects_path)
                active_id = projects.get("active")
                if active_id:
                    active_project = projects.get("projects", {}).get(active_id)
                    if active_project:
                        work_dir = active_project["path"]
                for agent in enabled_agents:
                    agent["work_dir"] = work_dir

                prompts = {
                    agent["name"]: PROMPT_TEMPLATE.format(
                        name=agent["name"].capitalize(),
                        context=context,
                        work_dir=work_dir,
                    )
                    for agent in enabled_agents
                }

                if acquire_lock(lock_path, f"relay-batch:{os.getpid()}", lock_ttl):
                    try:
                        # Prepend batch body to context via append
                        await append_reply(write_lock, log_path, "SYSTEM", batch_body)
                        await asyncio.gather(
                            *[
                                route_to(
                                    agent,
                                    prompts[agent["name"]],
                                    log_path,
                                    write_lock,
                                    sessions_path,
                                    session_lock,
                                    event_callback,
                                )
                                for agent in enabled_agents
                            ]
                        )
                    finally:
                        release_lock(lock_path)
            except Exception as exc:
                relay_log(f"batch processing error: {exc}")

    if socket_path.exists():
        socket_path.unlink()
        
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = await asyncio.start_unix_server(handle_client, path=str(socket_path))
    relay_log(f"listening on {socket_path}")

    batch_task = asyncio.create_task(batch_pending_tasks())
    try:
        await internal_stop_event.wait()
    finally:
        batch_task.cancel()
        try:
            await batch_task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()
        if socket_path.exists():
            socket_path.unlink()

    relay_log("stopped")
    emit_event(event_callback, {"type": "relay_state", "state": "stopped"})
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="clcod relay")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to config.json",
    )
    parser.add_argument(
        "--log",
        help="Override the configured shared log path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    if args.log:
        config["workspace"]["log_path"] = resolve_path(Path.cwd(), args.log)
    return asyncio.run(run_relay(config))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        relay_log("stopped")
