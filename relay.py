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

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "agents": [
        {
            "name": "CLAUDE",
            "enabled": True,
            "cmd": "claude",
            "args": ["-p"],
            "invoke_resume_args": ["-p", "--session-id", "{session_id}"],
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
}

PROMPT_TEMPLATE = (
    "You are {name}, one of three AI agents (Claude, Codex, Gemini) sharing a "
    "real-time terminal chat room. Below is the recent conversation log. "
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
    variables = {
        "script_dir": str(SCRIPT_DIR),
        "config_dir": str(config_dir),
    }

    normalized_workspace = {
        "log_path": resolve_path(config_dir, str(workspace.get("log_path", "clcodgemmix.txt"))),
        "lock_path": resolve_path(config_dir, str(workspace.get("lock_path", "speaker.lock"))),
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
    }
    variables.update(
        {
            "log_path": str(normalized_workspace["log_path"]),
            "lock_path": str(normalized_workspace["lock_path"]),
            "relay_log_path": str(normalized_workspace["relay_log_path"]),
            "pid_path": str(normalized_workspace["pid_path"]),
            "state_path": str(normalized_workspace["state_path"]),
            "sessions_path": str(normalized_workspace["sessions_path"]),
            "preferences_path": str(normalized_workspace["preferences_path"]),
            "agent_logs_dir": str(normalized_workspace["agent_logs_dir"]),
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
    }


def normalize_argv(value: Any, agent_name: str, field_name: str) -> list[str]:
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ValueError(f"{agent_name}: {field_name} must be a string or list")


def last_speaker(text: str) -> str:
    tags = re.findall(r"^\[([^\]]+)\]", text, re.MULTILINE)
    return tags[-1].strip() if tags else ""


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
    entry = f"\n[{speaker}]\n{text.strip()}\n"
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
    args = agent["args"]

    if not effective_session_id:
        effective_session_id = resolve_preseed_session_id(agent)

    if effective_session_id and agent["invoke_resume_args"]:
        args = [str(item).format_map({"session_id": effective_session_id}) for item in agent["invoke_resume_args"]]

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

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(SCRIPT_DIR),
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

    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if stderr_text:
        relay_log(f"{agent['name']}: stderr: {stderr_text}")

    if proc.returncode != 0:
        raise RuntimeError(f"exit code {proc.returncode}")

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
) -> int:
    workspace = config["workspace"]
    log_path: Path = workspace["log_path"]
    lock_path: Path = workspace["lock_path"]
    sessions_path: Path = workspace["sessions_path"]
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

    internal_stop_event = stop_event or asyncio.Event()
    if stop_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, internal_stop_event.set)
            except NotImplementedError:
                pass

    pending_size = 0

    while not internal_stop_event.is_set():
        try:
            await asyncio.wait_for(internal_stop_event.wait(), timeout=poll_sec)
            break
        except asyncio.TimeoutError:
            pass

        try:
            cur_size = log_path.stat().st_size
        except FileNotFoundError:
            log_path.touch(exist_ok=True)
            cur_size = 0

        if cur_size < last_size:
            last_size = 0
        if cur_size <= last_size:
            continue

        content = read_text(log_path)

        speaker = last_speaker(content)
        if not speaker or speaker.upper() in managed_names:
            pending_size = 0
            last_size = cur_size
            continue

        if pending_size != cur_size:
            emit_event(
                event_callback,
                {
                    "type": "transcript",
                    "last_speaker": speaker,
                    "last_updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            relay_log(f"[{speaker}] spoke -> {', '.join(agent['name'] for agent in enabled_agents)}")

        try:
            await asyncio.wait_for(internal_stop_event.wait(), timeout=activity_jitter(content))
            break
        except asyncio.TimeoutError:
            pass

        fresh = read_text(log_path)
        fresh_speaker = last_speaker(fresh)
        if not fresh_speaker or fresh_speaker.upper() in managed_names:
            pending_size = 0
            last_size = log_path.stat().st_size
            continue

        if not acquire_lock(lock_path, f"relay:{os.getpid()}", lock_ttl):
            pending_size = cur_size
            relay_log("speaker.lock is active; will retry this message")
            continue

        context = fresh[-context_len:]
        prompts = {
            agent["name"]: PROMPT_TEMPLATE.format(name=agent["name"].capitalize(), context=context)
            for agent in enabled_agents
        }

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
                    for agent in enabled_agents
                ]
            )
        finally:
            release_lock(lock_path)

        pending_size = 0
        last_size = log_path.stat().st_size

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
