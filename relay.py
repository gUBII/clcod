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
import errno
import fcntl
import json
import os
import re
import shlex
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import dispatcher as dispatcher_mod
from event_store import EventStore

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
            "model_options": ["default", "haiku", "sonnet", "opus"],
            "effort_options": ["default", "low", "medium", "high", "max"],
            "mirror_mode": "log",
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
            "timeout": 180,
        },
        {
            "name": "GEMINI",
            "enabled": True,
            "cmd": "gemini",
            "args": ["-y", "-p"],
            "model_arg": ["--model", "{value}"],
            "model_options": ["default", "gemini-3.1-pro-preview", "gemini-3-flash-preview", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
            "mirror_mode": "log",
            "preseed_session_id": False,
            "timeout": 120,
        },
    ],
    "workspace": {
        "log_path": "clcodgemmix.txt",
        "lock_path": "speaker.lock",
        "socket_path": ".clcod-runtime/room.sock",
        "poll_sec": 0.5,
        "context_len": 6000,
        "context_exclude_types": ["sync", "project"],  # Claude's suggestion
        "context_max_messages": 30,                    # Claude's suggestion
        "context_max_age_minutes": None,               # Optional: filter messages older than this
        "relay_log_path": ".clcod-runtime/relay.log",
        "pid_path": ".clcod-runtime/supervisor.pid",
        "state_path": ".clcod-runtime/state.json",
        "sessions_path": ".clcod-runtime/sessions.json",
        "preferences_path": ".clcod-runtime/preferences.json",
        "agent_logs_dir": ".clcod-runtime/agents",
        "projects_path": ".clcod-runtime/projects.json",
        "tasks_path": ".clcod-runtime/tasks.json",
        "events_db_path": ".clcod-runtime/events.db",
        "archives_dir": ".clcod-runtime/archives",
        "agent_roles": {}, # New: Store predefined role hints for agents
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

TASK_PROMPT_TEMPLATE = (
    "You are {name}, one of three AI agents (Claude, Codex, Gemini) sharing a "
    "real-time terminal chat room. Your working directory is {work_dir}. "
    "This is an explicit task assignment, not casual chat. Use the repository "
    "and your available tools to do the task now. Read files, inspect code "
    "paths, run commands, and make changes if the task asks for changes. Do "
    "not say you are still working, standing by, or verifying unless you "
    "actually did that work in this invocation and can name what you checked. "
    "If the task is review or verification, inspect the code directly and cite "
    "specific files or commands. If blocked, name the exact blocker.\n\n"
    "Task #{task_id}: {task_title}\n"
    "Task request:\n{task_request}\n\n"
    "Recent conversation log:\n{context}\n\n"
    "Reply with a concise execution report covering what you actually did, the "
    "result, and any blocker or next step."
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


def read_tail(path: Path, max_chars: int) -> str:
    """Read the last max_chars characters of a file without loading the whole file."""
    try:
        size = path.stat().st_size
        max_bytes = max_chars * 4  # 4x for multi-byte chars
        if size <= max_bytes:
            return path.read_text(encoding="utf-8", errors="replace")
        with path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(max(0, size - max_bytes))
            f.readline()  # skip partial line
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def parse_transcript_messages(raw: str) -> list[dict[str, Any]]:
    """Parse raw transcript string into a list of message dictionaries."""
    messages: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                messages.append(message)
        except json.JSONDecodeError:
            # Skip lines that are not valid JSON or are incomplete
            pass
    return messages


def filter_messages(
    msgs: list[dict[str, Any]],
    *,
    exclude_types: set[str] | None = None,
    max_age_minutes: int | None = None,
    max_messages: int | None = None,
) -> list[dict[str, Any]]:
    """Filter messages by type, age, and count."""
    filtered: list[dict[str, Any]] = []
    exclude_types = exclude_types or set()
    now_utc = datetime.now(timezone.utc)

    for message in reversed(msgs):  # Process from newest to oldest
        if message.get("type") in exclude_types:
            continue

        if max_age_minutes is not None:
            try:
                # Parse timestamp and make it timezone-aware (assuming UTC)
                msg_time = datetime.fromisoformat(message["ts"].replace("Z", "+00:00"))
                if now_utc - msg_time > timedelta(minutes=max_age_minutes):
                    continue  # Message is too old
            except (KeyError, ValueError):
                pass  # Ignore messages with malformed or missing timestamps

        filtered.append(message)
        if max_messages is not None and len(filtered) >= max_messages:
            break

    return list(reversed(filtered))  # Return in chronological order


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
            build_option("gemini-3.1-pro-preview"),
            build_option("gemini-3-flash-preview"),
            build_option("gemini-2.5-pro"),
            build_option("gemini-2.5-flash"),
            build_option("gemini-2.5-flash-lite"),
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
        "context_exclude_types": set(workspace.get("context_exclude_types", ["sync", "project"])),
        "context_max_messages": int(workspace.get("context_max_messages", 30)),
        "context_max_age_minutes": (
            int(workspace["context_max_age_minutes"])
            if workspace.get("context_max_age_minutes") is not None
            else None
        ),
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
        "events_db_path": resolve_path(
            config_dir, str(workspace.get("events_db_path", ".clcod-runtime/events.db"))
        ),
        "archives_dir": resolve_path(
            config_dir, str(workspace.get("archives_dir", ".clcod-runtime/archives"))
        ),
        "agent_roles": copy.deepcopy(workspace.get("agent_roles", {})),
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
            "events_db_path": str(normalized_workspace["events_db_path"]),
            "archives_dir": str(normalized_workspace["archives_dir"]),
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
            "max_sse_subscribers": int(ui.get("max_sse_subscribers", 32)),
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


def extract_target(body: str) -> str | None:
    """Extract explicit @MENTION target from message body.
    Returns the target name (e.g., 'CLAUDE') or None if no target found."""
    stripped = strip_task_prefix(body)
    match = re.match(r"^@(\w+)\b", stripped)
    if match:
        return match.group(1).upper()
    return None


def strip_task_prefix(body: str) -> str:
    stripped = body.strip()
    return re.sub(r"^/task\b", "", stripped, count=1, flags=re.IGNORECASE).strip()


def strip_target_prefix(body: str) -> str:
    stripped = body.strip()
    return re.sub(r"^@(\w+)\b[\s:,-]*", "", stripped, count=1, flags=re.IGNORECASE).strip()


def task_request_from_message(body: str) -> str:
    return strip_target_prefix(strip_task_prefix(body))


def build_structured_context(
    log_path: Path,
    context_len: int,
    *,
    task: dict[str, Any] | None = None,
    agent_name: str,
    exclude_types: set[str] | None = None,
    max_age_minutes: int | None = None,
    max_messages: int | None = None,
    role_hint: str | None = None,
) -> str:
    """Build a structured context string from the transcript log."""
    # Read more characters to ensure we capture full JSON lines near the end
    raw_log = read_tail(log_path, context_len * 4)
    messages = parse_transcript_messages(raw_log)
    filtered_messages = filter_messages(
        messages,
        exclude_types=exclude_types,
        max_age_minutes=max_age_minutes,
        max_messages=max_messages,
    )

    context_parts: list[str] = []

    # SECTION 1: System Brief
    context_parts.append("--- SYSTEM BRIEF ---")
    context_parts.append(f"You are {agent_name.capitalize()}.")
    # The actual work_dir will be passed to build_agent_prompt, this is a placeholder
    context_parts.append(f"Your working directory is {{work_dir}}.")
    if role_hint:
        context_parts.append(f"Your current role: {role_hint}")
    context_parts.append("--- END SYSTEM BRIEF ---")

    # SECTION 2: Active Task
    if task:
        context_parts.append("\n--- ACTIVE TASK ---")
        context_parts.append(f"Task ID: {task.get('id')}")
        context_parts.append(f"Task Title: {task.get('title')}")
        context_parts.append(f"Task Request: {task.get('request')}")
        context_parts.append("--- END ACTIVE TASK ---")

    # SECTION 3: Recent Messages
    context_parts.append("\n--- RECENT CONVERSATION LOG ---")
    for msg in filtered_messages:
        # Re-serialize each message to ensure proper formatting and handling of internal quotes
        context_parts.append(json.dumps(msg))
    context_parts.append("--- END RECENT CONVERSATION LOG ---")

    return "\n".join(context_parts)


def build_agent_prompt(
    *,
    agent_name: str,
    context: str,
    work_dir: str,
    task: dict[str, Any] | None = None,
) -> str:
    if task:
        task_request = str(task.get("request") or task.get("source_message") or task.get("title") or "").strip()
        return TASK_PROMPT_TEMPLATE.format(
            name=agent_name.capitalize(),
            context=context,
            work_dir=work_dir,
            task_id=task.get("id", "task"),
            task_title=task.get("title", "Untitled task"),
            task_request=task_request or "No task request provided.",
        )
    return PROMPT_TEMPLATE.format(
        name=agent_name.capitalize(),
        context=context,
        work_dir=work_dir,
    )


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
        pass


def persist_transcript_message(
    log_path: Path,
    speaker: str,
    text: str,
    *,
    event_callback: EventCallback | None = None,
    event_store: EventStore | None = None,
    message: dict[str, Any] | None = None,
    message_type: str = "message",
) -> dict[str, Any]:
    message = dict(message or {})
    message.setdefault("id", str(uuid.uuid4()))
    message.setdefault("sender", speaker)
    message.setdefault("seq", int(time.time() * 1000))
    message.setdefault("type", message_type)
    message["body"] = str(message.get("body", text)).strip()
    message.setdefault("ts", utc_now())

    entry = json.dumps(message) + "\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    max_lock_attempts = 10
    lock_backoff = 0.05  # 50ms

    with log_path.open("a", encoding="utf-8") as handle:
        for attempt in range(max_lock_attempts):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == max_lock_attempts - 1:
                    raise
                time.sleep(lock_backoff * (2 ** attempt))
        try:
            handle.write(entry)
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    emit_event(
        event_callback,
        {
            "type": "transcript",
            "correlation_id": message["id"],
            "sender": message["sender"],
            "last_speaker": message["sender"],
            "last_updated_at": message["ts"],
            "char_count": len(message["body"]),
            "message_type": message.get("type", message_type),
            "message": message,
        },
        event_store=event_store,
    )
    return message


def append_tagged_entry(
    log_path: Path,
    speaker: str,
    text: str,
    *,
    event_callback: EventCallback | None = None,
    event_store: EventStore | None = None,
    message: dict[str, Any] | None = None,
    message_type: str = "message",
) -> dict[str, Any]:
    return persist_transcript_message(
        log_path,
        speaker,
        text,
        event_callback=event_callback,
        event_store=event_store,
        message=message,
        message_type=message_type,
    )


async def append_reply(
    write_lock: asyncio.Lock,
    log_path: Path,
    speaker: str,
    text: str,
    *,
    event_callback: EventCallback | None = None,
    event_store: EventStore | None = None,
    message_type: str = "message",
) -> dict[str, Any]:
    async with write_lock:
        return persist_transcript_message(
            log_path,
            speaker,
            text,
            event_callback=event_callback,
            event_store=event_store,
            message_type=message_type,
        )


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


def update_task(tasks_path: Path, task_id: int, new_status: str) -> dict[str, Any] | None:
    """Update a single task's status. Returns the updated task or None if not found."""
    if new_status not in TASK_STATUSES:
        return None
    store = load_tasks(tasks_path)
    task = next((t for t in store["tasks"] if t["id"] == task_id), None)
    if task is None:
        return None
    task["status"] = new_status
    if new_status == "done":
        task["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_tasks(tasks_path, store)
    return task


def update_all_tasks(tasks_path: Path, new_status: str) -> list[dict[str, Any]]:
    """Move all non-done tasks to *new_status*. Returns the list of updated tasks."""
    if new_status not in TASK_STATUSES:
        return []
    store = load_tasks(tasks_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    updated: list[dict[str, Any]] = []
    for task in store["tasks"]:
        if task["status"] == new_status:
            continue
        task["status"] = new_status
        if new_status == "done":
            task["completed_at"] = now
        updated.append(task)
    if updated:
        save_tasks(tasks_path, store)
    return updated


def clear_all_tasks(tasks_path: Path) -> int:
    """Delete all tasks and reset the ID counter. Returns the count of cleared tasks."""
    store = load_tasks(tasks_path)
    count = len(store["tasks"])
    store["tasks"] = []
    store["next_id"] = 1
    save_tasks(tasks_path, store)
    return count


def load_sessions(path: Path) -> dict[str, str]:
    data = read_json(path, {})
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def save_sessions(path: Path, sessions: dict[str, str]) -> None:
    write_json(path, sessions)


def emit_event(
    event_callback: EventCallback | None,
    event: dict[str, Any],
    *,
    event_store: EventStore | None = None,
) -> dict[str, Any]:
    payload = dict(event)
    if event_store is not None:
        stored = event_store.append_event(payload)
        payload["event_id"] = stored["id"]
    if event_callback is not None:
        event_callback(payload)
    return payload


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def truncate_text(value: str, limit: int = 160) -> str:
    compact = " ".join(str(value).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def build_route_payload(
    *,
    sender: str,
    target: str,
    body: str,
    task: dict[str, Any] | None,
    source: str,
    requested_target: str | None = None,
    dispatcher_action: str | None = None,
    message_kind: str | None = None,
    batch_ids: list[int] | None = None,
) -> dict[str, Any]:
    is_task = body.strip().lower().startswith("/task")
    request_text = task_request_from_message(body) if is_task else body.strip()
    route_title = task.get("title") if task else truncate_text(request_text or body.strip(), 120)
    started_at = utc_now()
    return {
        "route_id": f"route-{uuid.uuid4().hex[:12]}",
        "task_id": task.get("id") if task else None,
        "task_title": route_title or "Untitled route",
        "body_preview": truncate_text(request_text or body.strip()),
        "sender": sender,
        "target": target,
        "source": source,
        "requested_target": requested_target,
        "dispatcher_action": dispatcher_action,
        "message_kind": message_kind or ("task" if is_task else "message"),
        "batch_ids": batch_ids or [],
        "started_at": started_at,
    }


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


def _build_sessionless_command(agent: dict[str, Any], prompt: str) -> list[str]:
    """Build a command with no session ID at all (uses base args, not invoke_resume_args)."""
    work_dir = agent.get("work_dir") or str(SCRIPT_DIR)
    variables = {"work_dir": work_dir, "script_dir": work_dir}
    args = [str(a).format_map(SafeFormatDict(variables)) for a in agent["args"]]
    selection_args = build_selection_args(agent)
    return [agent["cmd"], *selection_args, *args, prompt]


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


class CircuitBreaker:
    """Circuit breaker for agent dispatch with failure tracking and recovery.

    Implements the standard circuit breaker pattern:
    - Closed state: Requests pass through normally
    - Open state: Requests fail immediately after threshold failures
    - Half-open state: Allow one request to test recovery
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        reset_timeout: float = 300.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._failures: dict[str, int] = {}
        self._last_failure: dict[str, float] = {}

    def record_failure(self, agent_name: str) -> None:
        """Record a failure for an agent."""
        self._failures[agent_name] = self._failures.get(agent_name, 0) + 1
        self._last_failure[agent_name] = time.time()

    def record_success(self, agent_name: str) -> None:
        """Record a success for an agent (resets circuit)."""
        self._failures.pop(agent_name, None)
        self._last_failure.pop(agent_name, None)

    def is_open(self, agent_name: str) -> bool:
        """Check if the circuit is open for an agent.

        Returns False (half-open) if:
        - Failure count is below threshold, or
        - Enough time has passed since last failure (timeout exceeded)
        """
        failures = self._failures.get(agent_name, 0)
        if failures < self.failure_threshold:
            return False
        last = self._last_failure.get(agent_name, 0)
        if time.time() - last > self.reset_timeout:
            # Half-open: allow one attempt
            self._failures[agent_name] = self.failure_threshold - 1
            return False
        return True

    def reset_for_agent(self, agent_name: str) -> None:
        """Explicitly reset circuit for an agent (e.g., after operator intervention)."""
        self._failures.pop(agent_name, None)
        self._last_failure.pop(agent_name, None)


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

        # If fresh preseed also fails, fall back to sessionless invocation
        if returncode != 0 and _SESSION_IN_USE_RE.search(stderr_text):
            relay_log(f"{agent['name']}: fresh session also in use, retrying without session")
            current_session_id = None
            cmd = _build_sessionless_command(agent, prompt)
            log_agent_io(agent["io_log_path"], cmd, "", stderr_text, current_session_id)

            stdout, stderr, returncode = await _exec_agent(agent, cmd, env)
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                relay_log(f"{agent['name']}: stderr: {stderr_text}")

    # Salvage stdout even on non-zero exit when session-in-use was the only error
    # (Claude CLI can produce valid output then exit non-zero due to session cleanup)
    raw = stdout.decode("utf-8", errors="replace")
    if returncode != 0:
        if _SESSION_IN_USE_RE.search(stderr_text) and raw.strip():
            relay_log(f"{agent['name']}: non-zero exit with session error but stdout present, salvaging reply")
        else:
            raise RuntimeError(f"exit code {returncode}")
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
    route: dict[str, Any] | None = None,
    event_store: EventStore | None = None,
    circuit_breaker: "CircuitBreaker | None" = None,
) -> None:
    def publish(event: dict[str, Any]) -> dict[str, Any]:
        return emit_event(event_callback, event, event_store=event_store)

    name = agent["name"]
    route_started_at = route.get("started_at", utc_now()) if route else None
    msg: dict[str, Any] | None = None
    if route:
        publish(
            {
                "type": "route_state",
                **route,
                "started_at": route_started_at,
                "updated_at": route_started_at,
                "completed_at": None,
                "status": "transmitting",
                "tx_state": "active",
                "rx_state": "waiting",
                "last_error": None,
                "reply_chars": 0,
            },
        )
    publish(
        {"type": "agent_state", "agent": name, "state": "warming", "last_error": None},
    )

    # Check circuit breaker before dispatch
    cb = circuit_breaker
    if cb is not None and cb.is_open(name):
        relay_log(f"{name}: circuit open, skipping dispatch")
        failed_at = utc_now()
        publish(
            {
                "type": "route_state",
                **route,
                "started_at": route_started_at,
                "updated_at": failed_at,
                "completed_at": failed_at,
                "status": "error",
                "tx_state": "circuit_open",
                "rx_state": "error",
                "last_error": f"circuit breaker open for {name}",
                "reply_chars": 0,
            },
        )
        publish(
            {
                "type": "agent_state",
                "agent": name,
                "state": "circuit_open",
                "last_error": f"circuit breaker open for {name}",
            },
        )
        return

    try:
        result = await call_agent(agent, prompt, sessions_path, session_lock)
        if result.reply:
            msg = await append_reply(
                write_lock,
                log_path,
                name,
                result.reply,
                event_callback=event_callback,
                event_store=event_store,
            )
            relay_log(f"{name} replied ({len(result.reply)} chars)")
        else:
            relay_log(f"{name}: empty reply")
        # Record success for circuit breaker
        if cb is not None:
            cb.record_success(name)
        if route:
            completed_at = msg["ts"] if result.reply else utc_now()
            publish(
                {
                    "type": "route_state",
                    **route,
                    "started_at": route_started_at,
                    "updated_at": completed_at,
                    "completed_at": completed_at,
                    "status": "complete",
                    "tx_state": "sent",
                    "rx_state": "received" if result.reply else "empty",
                    "last_error": None,
                    "reply_chars": len(result.reply or ""),
                    "session_id": result.session_id,
                },
            )
        publish(
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
        if circuit_breaker is not None:
            circuit_breaker.record_failure(name)
        if route:
            failed_at = utc_now()
            publish(
                {
                    "type": "route_state",
                    **route,
                    "started_at": route_started_at,
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                    "status": "error",
                    "tx_state": "error",
                    "rx_state": "error",
                    "last_error": f"command not found: {agent['cmd']}",
                    "reply_chars": 0,
                },
            )
        publish(
            {
                "type": "agent_state",
                "agent": name,
                "state": "error",
                "last_error": f"command not found: {agent['cmd']}",
            },
        )
    except asyncio.TimeoutError:
        relay_log(f"{name}: timed out after {agent['timeout']}s")
        if circuit_breaker is not None:
            circuit_breaker.record_failure(name)
        if route:
            failed_at = utc_now()
            publish(
                {
                    "type": "route_state",
                    **route,
                    "started_at": route_started_at,
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                    "status": "error",
                    "tx_state": "sent",
                    "rx_state": "error",
                    "last_error": f"timed out after {agent['timeout']}s",
                    "reply_chars": 0,
                },
            )
        publish(
            {
                "type": "agent_state",
                "agent": name,
                "state": "error",
                "last_error": f"timed out after {agent['timeout']}s",
            },
        )
    except Exception as exc:
        relay_log(f"{name}: error: {exc}")
        if route:
            failed_at = utc_now()
            publish(
                {
                    "type": "route_state",
                    **route,
                    "started_at": route_started_at,
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                    "status": "error",
                    "tx_state": "sent",
                    "rx_state": "error",
                    "last_error": str(exc),
                    "reply_chars": 0,
                },
            )
        publish(
            {
                "type": "agent_state",
                "agent": name,
                "state": "error",
                "last_error": str(exc),
            },
        )
        if circuit_breaker is not None:
            circuit_breaker.record_failure(name)

    # Also catch RuntimeError from call_agent (e.g., non-zero exit code)
    except RuntimeError as exc:
        relay_log(f"{name}: {exc}")
        if circuit_breaker is not None:
            circuit_breaker.record_failure(name)
        if route:
            failed_at = utc_now()
            publish(
                {
                    "type": "route_state",
                    **route,
                    "started_at": route_started_at,
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                    "status": "error",
                    "tx_state": "sent",
                    "rx_state": "error",
                    "last_error": str(exc),
                    "reply_chars": 0,
                },
            )
        publish(
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
    event_store: EventStore | None = None,
) -> int:
    workspace = config["workspace"]
    log_path: Path = workspace["log_path"]
    lock_path: Path = workspace["lock_path"]
    sessions_path: Path = workspace["sessions_path"]
    projects_path: Path = workspace["projects_path"]
    tasks_path: Path = workspace["tasks_path"]
    poll_sec = workspace["poll_sec"]
    context_len = workspace["context_len"]
    context_exclude_types = workspace.get("context_exclude_types", set())
    context_max_messages = workspace.get("context_max_messages")
    context_max_age_minutes = workspace.get("context_max_age_minutes")
    agent_roles = workspace.get("agent_roles", {})
    lock_ttl = config["locks"]["ttl"]
    enabled_agents = [agent for agent in config["agents"] if agent["enabled"]]
    managed_names = {agent["name"] for agent in enabled_agents}
    write_lock = asyncio.Lock()
    session_lock = asyncio.Lock()
    owns_event_store = False

    # Initialize circuit breaker with configurable thresholds
    cb_config = config.get("locks", {}).get("circuit_breaker", {})
    circuit_breaker = CircuitBreaker(
        failure_threshold=cb_config.get("failure_threshold", 3),
        reset_timeout=cb_config.get("reset_timeout", 300.0),
    )
    relay_log(f"circuit breaker: threshold={circuit_breaker.failure_threshold}, reset={circuit_breaker.reset_timeout}s")

    if not enabled_agents:
        raise ValueError("no enabled agents configured")

    if event_store is None:
        event_store = EventStore(workspace["events_db_path"])
        owns_event_store = True

    def publish(event: dict[str, Any]) -> dict[str, Any]:
        return emit_event(event_callback, event, event_store=event_store)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    if not sessions_path.exists():
        save_sessions(sessions_path, {})
    relay_log(f"watching {log_path}")
    relay_log(f"routing to: {', '.join(agent['name'] for agent in enabled_agents)}")
    relay_log(
        "poll="
        f"{poll_sec}s context_len={context_len} lock_ttl={lock_ttl}s "
        f"timeouts={[agent['timeout'] for agent in enabled_agents]}"
    )
    publish({"type": "relay_state", "state": "running"})
    for agent in enabled_agents:
        publish(
            {
                "type": "agent_state",
                "agent": agent["name"],
                "state": "starting",
                "mirror_mode": agent["mirror_mode"],
            },
        )

    socket_path: Path = workspace["socket_path"]
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
            
            incoming = json.loads(data.decode("utf-8"))
            sender = incoming.get("sender", "UNKNOWN")
            body = incoming.get("body", "")
            if not body:
                return

            message_type = str(incoming.get("type") or "message")
            incoming_message_id = incoming.get("id")
            payload = persist_transcript_message(
                log_path,
                sender,
                body,
                event_callback=event_callback,
                event_store=event_store,
                message=incoming,
                message_type=message_type,
            )

            if message_type != "message":
                relay_log(f"[{sender}] system message ({message_type}) saved — skipping dispatch")
                return

            stripped = body.strip()
            lower = stripped.lower()

            if lower == "/clearall":
                count = clear_all_tasks(tasks_path)
                relay_log(f"/clearall: {count} task(s) removed, board reset")
                publish({"type": "tasks_cleared", "tasks": []})
                return

            if lower.startswith("/moveall "):
                target_status = stripped.split(None, 1)[1].strip().lower().replace(" ", "_")
                if target_status not in TASK_STATUSES:
                    relay_log(f"/moveall: invalid status '{target_status}'")
                else:
                    updated = update_all_tasks(tasks_path, target_status)
                    relay_log(f"/moveall -> {target_status}: {len(updated)} task(s) updated")
                    publish(
                        {
                            "type": "tasks_updated",
                            "tasks": updated,
                            "new_status": target_status,
                        }
                    )
                return

            if lower.startswith("/move "):
                parts = stripped.split()
                if len(parts) >= 3:
                    try:
                        move_id = int(parts[1].lstrip("#"))
                    except ValueError:
                        relay_log(f"/move: invalid task id '{parts[1]}'")
                        return
                    target_status = parts[2].lower().replace(" ", "_")
                    if target_status not in TASK_STATUSES:
                        relay_log(f"/move: invalid status '{target_status}'")
                        return
                    task = update_task(tasks_path, move_id, target_status)
                    if task:
                        relay_log(f"/move #{move_id} -> {target_status}")
                        publish({"type": "task_updated", "task": task})
                    else:
                        relay_log(f"/move: task #{move_id} not found")
                return

            if lower == "/context-check":
                relay_log(f"[{sender}] /context-check command received")

                # Determine which agent's context to inspect
                target_agent_name = sender.upper() if sender != "Operator" else "GEMINI" # Default to GEMINI if Operator

                # Retrieve config values from the outer scope
                # log_path, context_len, context_exclude_types, context_max_messages, context_max_age_minutes, agent_roles, tasks_path are available from run_relay's scope

                # Find an agent definition
                agent_config = next((a for a in enabled_agents if a["name"] == target_agent_name), None)
                if not agent_config:
                    await append_reply(
                        write_lock,
                        log_path,
                        "SYSTEM",
                        f"Could not find configuration for agent {target_agent_name}.",
                        event_callback=event_callback,
                        event_store=event_store,
                    )
                    return

                # Generate the structured context string
                current_work_dir = str(SCRIPT_DIR) # Default, will be overridden if project is active
                projects = load_projects(projects_path)
                active_id = projects.get("active")
                if active_id:
                    active_project = projects.get("projects", {}).get(active_id)
                    if active_project:
                        current_work_dir = active_project["path"]

                # Find active task for the agent
                active_task_info = None
                all_tasks = load_tasks(tasks_path)["tasks"]
                for t in all_tasks:
                    if t["status"] == "in_progress" and target_agent_name in t.get("assigned_to", []):
                        active_task_info = {"id": t["id"], "title": t["title"], "request": t["source_message"]}
                        break
                # If no specific task for this agent, check for any in-progress task
                if not active_task_info:
                     for t in all_tasks:
                        if t["status"] == "in_progress":
                            active_task_info = {"id": t["id"], "title": t["title"], "request": t["source_message"]}
                            break

                role_hint = agent_roles.get(target_agent_name)

                # The work_dir placeholder in build_structured_context is formatted outside
                structured_context_raw = build_structured_context(
                    log_path,
                    context_len,
                    task=active_task_info,
                    agent_name=target_agent_name,
                    exclude_types=context_exclude_types,
                    max_age_minutes=context_max_age_minutes,
                    max_messages=context_max_messages,
                    role_hint=role_hint,
                )
                structured_context = structured_context_raw.format(work_dir=current_work_dir)


                # Analyze raw log for metrics
                full_raw_log = read_text(log_path) # Read entire log file
                all_messages = parse_transcript_messages(full_raw_log)
                initial_message_count = len(all_messages)

                filtered_for_metrics = filter_messages(
                    all_messages,
                    exclude_types=context_exclude_types,
                    max_age_minutes=context_max_age_minutes,
                    max_messages=context_max_messages,
                )
                messages_included_count = len(filtered_for_metrics)
                messages_filtered_count = initial_message_count - messages_included_count

                oldest_message_ts = filtered_for_metrics[0]["ts"] if filtered_for_metrics else "N/A"

                # Calculate metrics
                total_chars = len(structured_context)
                estimated_tokens = total_chars // 4 # Common heuristic

                report_parts = [
                    f"--- CONTEXT AUDIT REPORT for {target_agent_name} ---",
                    f"Total Characters in Context Window: {total_chars}",
                    f"Estimated Tokens (heuristic): {estimated_tokens}",
                    f"Messages Included: {messages_included_count}",
                    f"Messages Filtered Out: {messages_filtered_count} (types: {', '.join(context_exclude_types) if context_exclude_types else 'None'})",
                    f"Oldest Message Timestamp: {oldest_message_ts}",
                    f"Active Task: {active_task_info['title']} (ID: {active_task_info['id']})" if active_task_info else "Active Task: None",
                    f"Agent Role Hint: {role_hint or 'None'}",
                    "--- END REPORT ---",
                ]
                report = "\n".join(report_parts)
                relay_log(report) # Log to relay log

                # Publish as SYSTEM message
                await append_reply(
                    write_lock,
                    log_path,
                    "SYSTEM",
                    report,
                    event_callback=event_callback,
                    event_store=event_store,
                )
                return

            explicit_target = extract_target(body)
            valid_agent_names = {agent["name"] for agent in enabled_agents}
            dispatch_config = config.get("dispatcher", {})

            if is_sleeping and is_sleeping():
                relay_log(f"sleeping — message from {sender} saved but not routed")
                return

            projects = load_projects(projects_path)
            active_id = projects.get("active")
            active_project = projects.get("projects", {}).get(active_id or "") if active_id else None
            work_dir = active_project["path"] if active_project else str(SCRIPT_DIR)

            for agent in enabled_agents:
                agent["work_dir"] = work_dir

            context = read_tail(log_path, context_len)

            dispatch_targets = enabled_agents
            requested_target = None
            dispatcher_decision = None

            if explicit_target and explicit_target.upper() == "DISPATCHER":
                stripped_body = strip_target_prefix(body)
                relay_log(f"[{sender}] @DISPATCHER direct query: {stripped_body[:80]}")
                if dispatch_config.get("enabled"):
                    try:
                        decision = await dispatcher_mod.classify_message(stripped_body, context, dispatch_config)
                        action = decision.get("action", "route")
                        targets = decision.get("targets", [])
                        task_type = decision.get("task_type", "")
                        priority = decision.get("priority", "")
                        reply_text = decision.get("reply") or (
                            f"I would route this to: {', '.join(targets)}. "
                            f"Task type: {task_type}. Priority: {priority}."
                        )
                        await append_reply(
                            write_lock,
                            log_path,
                            "DISPATCHER",
                            reply_text,
                            event_callback=event_callback,
                            event_store=event_store,
                        )
                        relay_log(f"dispatcher direct reply: {reply_text[:80]}")
                    except Exception as exc:
                        relay_log(f"dispatcher direct query failed: {exc}")
                else:
                    await append_reply(
                        write_lock,
                        log_path,
                        "DISPATCHER",
                        "Dispatcher is disabled in config.",
                        event_callback=event_callback,
                        event_store=event_store,
                    )
                return

            if explicit_target:
                if explicit_target in valid_agent_names:
                    dispatch_targets = [a for a in enabled_agents if a["name"] == explicit_target]
                    requested_target = explicit_target
                    relay_log(f"[{sender}] @{explicit_target} -> {explicit_target}")
                else:
                    relay_log(f"[{sender}] mentioned unknown agent @{explicit_target} (valid: {', '.join(sorted(valid_agent_names))})")
                    dispatch_targets = enabled_agents
                    requested_target = explicit_target
            else:
                if dispatch_config.get("enabled"):
                    try:
                        dispatcher_decision = await dispatcher_mod.classify_message(body, context, dispatch_config)
                        action = dispatcher_decision.get("action", "route")
                        relay_log(f"dispatcher: action={action} targets={dispatcher_decision.get('targets')} type={dispatcher_decision.get('task_type')}")
                        dispatcher_event: dict[str, Any] = {
                            "type": "dispatcher",
                            "action": action,
                            "targets": dispatcher_decision.get("targets", []),
                        }
                        # Only include task metadata when the message is an explicit /task command
                        if body.strip().lower().startswith("/task"):
                            dispatcher_event["task_type"] = dispatcher_decision.get("task_type")
                            dispatcher_event["priority"] = dispatcher_decision.get("priority")
                        publish(dispatcher_event)

                        is_slash_task = lower.startswith("/task")

                        if action == "absorb" and dispatcher_decision.get("reply"):
                            await append_reply(
                                write_lock,
                                log_path,
                                "DISPATCHER",
                                dispatcher_decision["reply"],
                                event_callback=event_callback,
                                event_store=event_store,
                            )
                            relay_log(f"dispatcher absorbed: {dispatcher_decision['reply'][:80]}")
                            if not is_slash_task:
                                return

                        if action == "clarify" and dispatcher_decision.get("reply"):
                            await append_reply(
                                write_lock,
                                log_path,
                                "DISPATCHER",
                                dispatcher_decision["reply"],
                                event_callback=event_callback,
                                event_store=event_store,
                            )
                            relay_log(f"dispatcher clarifying: {dispatcher_decision['reply'][:80]}")
                            if not is_slash_task:
                                return

                        if action == "route" and dispatcher_decision.get("targets"):
                            target_names = set(dispatcher_decision["targets"])
                            filtered = [a for a in enabled_agents if a["name"] in target_names]
                            if filtered:
                                dispatch_targets = filtered
                                requested_target = " ".join(target_names)
                                relay_log(f"dispatcher routed to: {', '.join(a['name'] for a in dispatch_targets)}")
                    except Exception as exc:
                        relay_log(f"dispatcher error (falling back to broadcast): {exc}")
                else:
                    relay_log(f"[{sender}] spoke -> {', '.join(agent['name'] for agent in enabled_agents)}")

            task: dict[str, Any] | None = None
            task_prompt: dict[str, Any] | None = None
            if body.strip().lower().startswith("/task"):
                task_body = task_request_from_message(body)
                task_title = task_body[:120] if len(task_body) <= 120 else task_body[:117] + "..."
                if not task_title:
                    task_title = "Untitled task"
                task_type = "general"
                task_priority = "normal"
                if dispatcher_decision:
                    task_type = dispatcher_decision.get("task_type", "general")
                    task_priority = dispatcher_decision.get("priority", "normal")
                task = create_task(
                    tasks_path,
                    title=task_title,
                    task_type=task_type,
                    priority=task_priority,
                    assigned_to=[a["name"] for a in dispatch_targets],
                    source_message=body,
                )
                relay_log(f"task #{task['id']} created: {task['title'][:60]}")
                publish({"type": "task_created", "task": task})
                task_prompt = {
                    "id": task["id"],
                    "title": task_title,
                    "request": task_body or body.strip(),
                    "source_message": body,
                }

            route_source = "broadcast"
            if explicit_target and explicit_target in valid_agent_names:
                route_source = "mention"
            elif dispatcher_decision is not None:
                route_source = "dispatcher"

            target_names = [a["name"] for a in dispatch_targets]
            queued = event_store.enqueue_dispatch({
                "sender": sender,
                "body": body,
                "targets": target_names,
                "task": task_prompt,
                "route_source": route_source,
                "requested_target": requested_target,
                "dispatcher_action": dispatcher_decision.get("action") if dispatcher_decision else None,
                "work_dir": work_dir,
                "message_id": incoming_message_id,
                "message_kind": "task" if task else "message",
            })
            relay_log(f"dispatch queued (job #{queued['id']}): {', '.join(target_names)}")
            publish({
                "type": "dispatch_queued",
                "job_id": queued["id"],
                "targets": target_names,
                "sender": sender,
                "message_kind": "task" if task else "message",
                "queue_depth": event_store.queue_depth(),
            })

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
                batch_body_parts = []
                batch_ids = []
                updated_tasks = []
                for task in pending[:5]:
                    batch_body_parts.append(f"- [{task['type']}] {task['title']}")
                    batch_ids.append(task["id"])
                    task["status"] = "in_progress"
                    task["assigned_to"] = [a["name"] for a in enabled_agents]
                    updated_tasks.append(dict(task))
                batch_body = "Batch tasks:\n" + "\n".join(batch_body_parts)
                save_tasks(tasks_path, store)
                relay_log(f"batch processing {len(batch_ids)} pending tasks: {batch_ids}")
                publish(
                    {
                        "type": "tasks_updated",
                        "tasks": updated_tasks,
                        "new_status": "in_progress",
                    },
                )

                work_dir = str(SCRIPT_DIR)
                projects = load_projects(projects_path)
                active_id = projects.get("active")
                if active_id:
                    active_project = projects.get("projects", {}).get(active_id)
                    if active_project:
                        work_dir = active_project["path"]
                for agent in enabled_agents:
                    agent["work_dir"] = work_dir

                await append_reply(
                    write_lock,
                    log_path,
                    "SYSTEM",
                    batch_body,
                    event_callback=event_callback,
                    event_store=event_store,
                    message_type="batch",
                )
                batch_targets = [a["name"] for a in enabled_agents]
                queued = event_store.enqueue_dispatch({
                    "sender": "SYSTEM",
                    "body": batch_body,
                    "targets": batch_targets,
                    "task": {
                        "id": "batch",
                        "title": f"Batch tasks ({len(batch_ids)})",
                        "request": batch_body,
                    },
                    "route_source": "batch",
                    "message_kind": "batch",
                    "batch_ids": batch_ids,
                    "work_dir": work_dir,
                })
                relay_log(f"batch dispatch queued (job #{queued['id']})")
                publish({
                    "type": "dispatch_queued",
                    "job_id": queued["id"],
                    "targets": batch_targets,
                    "sender": "SYSTEM",
                    "message_kind": "batch",
                    "batch_ids": batch_ids,
                    "queue_depth": event_store.queue_depth(),
                })
            except Exception as exc:
                relay_log(f"batch processing error: {exc}")

    async def dispatch_drain_loop() -> None:
        """Process queued dispatch jobs sequentially, replacing the old speaker.lock mechanism."""
        # On startup, recover ALL active jobs from a previous crash (they can't be in-flight)
        recovered = event_store.recover_stale_active(max_age_seconds=0)
        if recovered:
            relay_log(f"recovered {recovered} stale active dispatch job(s)")

        while not internal_stop_event.is_set():
            job: dict[str, Any] | None = None
            try:
                job = event_store.claim_next_dispatch()
                if not job:
                    await asyncio.sleep(0.3)
                    continue

                target_names = set(job["targets"])
                target_agents = [a for a in enabled_agents if a["name"] in target_names]
                if not target_agents:
                    event_store.complete_dispatch(job["id"], "failed", "no matching agents")
                    publish({
                        "type": "dispatch_failed",
                        "job_id": job["id"],
                        "targets": job["targets"],
                        "error": "no matching agents",
                        "queue_depth": event_store.queue_depth(),
                    })
                    relay_log(f"dispatch job #{job['id']} failed: no matching agents for {job['targets']}")
                    continue

                work_dir = job.get("work_dir") or str(SCRIPT_DIR)
                for agent in target_agents:
                    agent["work_dir"] = work_dir

                task = job.get("task")
                prompts = {
                    agent["name"]: build_agent_prompt(
                        agent_name=agent["name"],
                        context=build_structured_context(
                            log_path,
                            context_len,
                            task=task,
                            agent_name=agent["name"],
                            exclude_types=context_exclude_types,
                            max_age_minutes=context_max_age_minutes,
                            max_messages=context_max_messages,
                            role_hint=agent_roles.get(agent["name"]),
                        ).format(work_dir=work_dir),
                        work_dir=work_dir,
                        task=task,
                    )
                    for agent in target_agents
                }
                route_payloads = {
                    agent["name"]: build_route_payload(
                        sender=job.get("sender", "UNKNOWN"),
                        target=agent["name"],
                        body=job["body"],
                        task=task,
                        source=job.get("route_source", "broadcast"),
                        requested_target=job.get("requested_target"),
                        dispatcher_action=job.get("dispatcher_action"),
                        batch_ids=job.get("batch_ids", []),
                        message_kind=job.get("message_kind"),
                    )
                    for agent in target_agents
                }

                if task and isinstance(task.get("id"), int):
                    updated_task = update_task(tasks_path, task["id"], "in_progress")
                    if updated_task:
                        publish({"type": "task_updated", "task": updated_task})

                publish({
                    "type": "dispatch_started",
                    "job_id": job["id"],
                    "targets": job["targets"],
                    "sender": job.get("sender"),
                    "queue_depth": event_store.queue_depth(),
                })

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
                            route_payloads[agent["name"]],
                            event_store=event_store,
                            circuit_breaker=circuit_breaker,
                        )
                        for agent in target_agents
                    ]
                )

                event_store.complete_dispatch(job["id"], "done")
                publish({
                    "type": "dispatch_completed",
                    "job_id": job["id"],
                    "targets": job["targets"],
                    "queue_depth": event_store.queue_depth(),
                })
                relay_log(f"dispatch completed (job #{job['id']})")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if job:
                    event_store.complete_dispatch(job["id"], "failed", str(exc))
                    publish({
                        "type": "dispatch_failed",
                        "job_id": job["id"],
                        "targets": job.get("targets", []),
                        "error": str(exc),
                        "queue_depth": event_store.queue_depth(),
                    })
                relay_log(f"dispatch drain error: {exc}")

    if socket_path.exists():
        socket_path.unlink()

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = await asyncio.start_unix_server(handle_client, path=str(socket_path))
    relay_log(f"listening on {socket_path}")

    batch_task = asyncio.create_task(batch_pending_tasks())
    drain_task = asyncio.create_task(dispatch_drain_loop())
    try:
        await internal_stop_event.wait()
    finally:
        batch_task.cancel()
        drain_task.cancel()
        try:
            await batch_task
        except asyncio.CancelledError:
            pass
        try:
            await drain_task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()
        if socket_path.exists():
            socket_path.unlink()

    relay_log("stopped")
    publish({"type": "relay_state", "state": "stopped"})
    if owns_event_store and event_store is not None:
        event_store.close()
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
