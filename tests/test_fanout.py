"""
Tests for S5: fan-out reliability — straggler isolation.

Covers:
  1. 3-target fan-out where ONE agent raises → other two still return results
     (return_exceptions path via route_to exception isolation).
  2. 3-target fan-out where ONE agent exceeds its per-agent timeout → it yields
     a "timed out" terminal agent_state, the other two are unaffected.
  3. Per-agent timeout override resolves from config (engine-specific value used,
     global default_timeout fallback when unset).
  4. Circuit-breaker open → that engine short-circuits without spawning a subprocess.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import relay


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _agent(name: str, timeout: int = 30) -> dict[str, Any]:
    """Minimal agent config for route_to / call_agent tests."""
    bases: dict[str, Any] = {
        "CLAUDE": {
            "cmd": "claude",
            "args": ["-p", "--dangerously-skip-permissions"],
            "invoke_resume_args": ["-p", "--dangerously-skip-permissions", "--session-id", "{session_id}"],
            "mirror_mode": "log",
            "preseed_session_id": False,
        },
        "CODEX": {
            "cmd": "codex",
            "args": ["exec", "--json", "--skip-git-repo-check", "-C", "/tmp"],
            "invoke_resume_args": ["exec", "--json", "--skip-git-repo-check", "-C", "/tmp", "resume", "{session_id}"],
            "mirror_mode": "resume",
            "preseed_session_id": False,
        },
        "GEMINI": {
            "cmd": "gemini",
            "args": ["-y", "-o", "stream-json", "-p"],
            "invoke_resume_args": ["-y", "--session-id", "{session_id}", "-o", "stream-json", "-p"],
            "mirror_mode": "log",
            "preseed_session_id": False,
        },
    }
    b = bases.get(name, bases["CLAUDE"])
    return {
        "name": name,
        "enabled": True,
        "work_dir": "/tmp",
        "timeout": timeout,
        "io_log_path": None,
        "model_arg": [],
        "effort_arg": [],
        "model_options": [],
        "effort_options": [],
        "grpc_target": "",
        **b,
    }


def _events_sink() -> list[dict[str, Any]]:
    return []


def _make_event_callback(sink: list[dict[str, Any]]):
    def cb(event: dict[str, Any]) -> None:
        sink.append(event)
    return cb


# ─────────────────────────────────────────────────────────────────────────────
# 1. Fan-out independence: one exception does not cancel peers
# ─────────────────────────────────────────────────────────────────────────────


class TestFanoutExceptionIsolation(unittest.TestCase):
    """asyncio.gather with return_exceptions=True: one failing route_to does not block peers."""

    def test_one_raise_others_still_return(self):
        """When one agent's call_agent raises, the other two complete normally."""

        agents = [_agent("CLAUDE"), _agent("CODEX"), _agent("GEMINI")]
        events: list[dict[str, Any]] = _events_sink()
        cb = _make_event_callback(events)

        replies: list[str] = []

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                log_path = Path(tmp) / "log.txt"
                log_path.touch()
                sessions_path = Path(tmp) / "sessions.json"
                write_lock = asyncio.Lock()
                session_lock = asyncio.Lock()

                call_count = [0]

                async def fake_call_agent(agent, prompt, sp, sl, token_callback=None):
                    call_count[0] += 1
                    name = agent["name"]
                    if name == "CODEX":
                        raise RuntimeError("CODEX exploded")
                    return relay.AgentCallResult(
                        reply=f"{name}-ok", raw=f"{name}-ok", stderr="", session_id=None
                    )

                with (
                    mock.patch.object(relay, "call_agent", side_effect=fake_call_agent),
                    mock.patch.object(relay, "log_agent_io", return_value=None),
                ):
                    await asyncio.gather(
                        *[
                            relay.route_to(
                                ag,
                                "hello",
                                log_path,
                                write_lock,
                                sessions_path,
                                session_lock,
                                cb,
                                route=None,
                                event_store=None,
                                circuit_breaker=None,
                            )
                            for ag in agents
                        ],
                        return_exceptions=True,
                    )

                # Collect replies written to the log
                raw = log_path.read_text()
                for line in raw.splitlines():
                    try:
                        msg = json.loads(line)
                        if msg.get("type") == "message":
                            replies.append(msg.get("body", ""))
                    except json.JSONDecodeError:
                        pass

        asyncio.run(run())

        # CLAUDE and GEMINI must have replied; CODEX must not have written a reply
        self.assertIn("CLAUDE-ok", replies, "CLAUDE must reply even when CODEX raises")
        self.assertIn("GEMINI-ok", replies, "GEMINI must reply even when CODEX raises")
        codex_replies = [r for r in replies if "CODEX" in r]
        self.assertEqual(codex_replies, [], "CODEX raised — must not have a reply")

        # The error event for CODEX must have been emitted
        error_agents = [e["agent"] for e in events if e.get("type") == "agent_state" and e.get("state") == "error"]
        self.assertIn("CODEX", error_agents, "CODEX must emit an error agent_state event")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Per-agent timeout isolation: one timeout does not affect peers
# ─────────────────────────────────────────────────────────────────────────────


class TestFanoutTimeoutIsolation(unittest.TestCase):
    """One agent's asyncio.TimeoutError yields a per-agent terminal status; peers are unaffected."""

    def test_one_timeout_others_complete(self):
        agents = [_agent("CLAUDE", timeout=5), _agent("CODEX", timeout=1), _agent("GEMINI", timeout=5)]
        events: list[dict[str, Any]] = _events_sink()
        cb = _make_event_callback(events)

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                log_path = Path(tmp) / "log.txt"
                log_path.touch()
                sessions_path = Path(tmp) / "sessions.json"
                write_lock = asyncio.Lock()
                session_lock = asyncio.Lock()

                async def fake_call_agent(agent, prompt, sp, sl, token_callback=None):
                    name = agent["name"]
                    if name == "CODEX":
                        raise asyncio.TimeoutError
                    return relay.AgentCallResult(
                        reply=f"{name}-ok", raw=f"{name}-ok", stderr="", session_id=None
                    )

                with (
                    mock.patch.object(relay, "call_agent", side_effect=fake_call_agent),
                    mock.patch.object(relay, "log_agent_io", return_value=None),
                ):
                    await asyncio.gather(
                        *[
                            relay.route_to(
                                ag, "hello", log_path, write_lock,
                                sessions_path, session_lock, cb,
                                route=None, event_store=None, circuit_breaker=None,
                            )
                            for ag in agents
                        ],
                        return_exceptions=True,
                    )

        asyncio.run(run())

        # CODEX must emit a timeout error agent_state
        codex_errors = [
            e for e in events
            if e.get("type") == "agent_state" and e.get("agent") == "CODEX"
            and e.get("state") == "error"
        ]
        self.assertTrue(codex_errors, "CODEX must emit an error agent_state on timeout")
        self.assertIn("timed out", codex_errors[0].get("last_error", ""),
                      "CODEX error must mention 'timed out'")

        # CLAUDE and GEMINI must reach "ready" state
        ready_agents = {
            e["agent"] for e in events
            if e.get("type") == "agent_state" and e.get("state") == "ready"
        }
        self.assertIn("CLAUDE", ready_agents, "CLAUDE must reach ready state unaffected by CODEX timeout")
        self.assertIn("GEMINI", ready_agents, "GEMINI must reach ready state unaffected by CODEX timeout")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Per-agent timeout resolves from config with global fallback
# ─────────────────────────────────────────────────────────────────────────────


class TestPerAgentTimeoutConfig(unittest.TestCase):
    """Engine-specific timeout is used; global default_timeout is the fallback."""

    def _config_with_timeouts(
        self,
        agent_timeouts: dict[str, int | None],
        default_timeout: int | None = None,
    ) -> dict[str, Any]:
        """Build a minimal config dict and run load_config's normalization logic."""
        agents_raw = []
        for name, t in agent_timeouts.items():
            entry: dict[str, Any] = {
                "name": name,
                "cmd": name.lower(),
                "args": [],
            }
            if t is not None:
                entry["timeout"] = t
            agents_raw.append(entry)

        locks_raw: dict[str, Any] = {}
        if default_timeout is not None:
            locks_raw["default_timeout"] = default_timeout

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"agents": agents_raw, "locks": locks_raw}, f)
            cfg_path = f.name

        return relay.load_config(cfg_path)

    def test_engine_specific_timeout_used(self):
        cfg = self._config_with_timeouts(
            {"GEMINI": 300, "CODEX": 90},
            default_timeout=120,
        )
        by_name = {a["name"]: a for a in cfg["agents"]}
        self.assertEqual(by_name["GEMINI"]["timeout"], 300)
        self.assertEqual(by_name["CODEX"]["timeout"], 90)

    def test_global_default_timeout_fallback(self):
        cfg = self._config_with_timeouts(
            {"CLAUDE": None},  # no per-agent timeout
            default_timeout=75,
        )
        by_name = {a["name"]: a for a in cfg["agents"]}
        self.assertEqual(by_name["CLAUDE"]["timeout"], 75)

    def test_hardcoded_fallback_when_no_default(self):
        cfg = self._config_with_timeouts(
            {"CLAUDE": None},  # no per-agent timeout, no global default
            default_timeout=None,
        )
        by_name = {a["name"]: a for a in cfg["agents"]}
        # Hardcoded fallback in load_config is 60
        self.assertEqual(by_name["CLAUDE"]["timeout"], 60)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Circuit breaker open → short-circuit without subprocess
# ─────────────────────────────────────────────────────────────────────────────


class TestCircuitBreakerFanout(unittest.TestCase):
    """An open circuit breaker short-circuits to 'circuit_open' without spawning a subprocess."""

    def test_open_breaker_no_subprocess(self):
        ag = _agent("GEMINI")
        events: list[dict[str, Any]] = _events_sink()
        cb_fn = _make_event_callback(events)

        # Trip the breaker above threshold (failure_threshold=2 in new defaults)
        breaker = relay.CircuitBreaker(failure_threshold=2, reset_timeout=9999.0)
        breaker.record_failure("GEMINI")
        breaker.record_failure("GEMINI")

        self.assertTrue(breaker.is_open("GEMINI"), "breaker must be open after 2 failures")

        subprocess_calls = [0]

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                log_path = Path(tmp) / "log.txt"
                log_path.touch()
                sessions_path = Path(tmp) / "sessions.json"
                write_lock = asyncio.Lock()
                session_lock = asyncio.Lock()

                async def fake_exec(*args, **kwargs):
                    subprocess_calls[0] += 1
                    return b"", b"", 0

                with mock.patch.object(relay, "_exec_agent", side_effect=fake_exec):
                    await relay.route_to(
                        ag, "hello", log_path, write_lock,
                        sessions_path, session_lock, cb_fn,
                        route=None, event_store=None, circuit_breaker=breaker,
                    )

        asyncio.run(run())

        self.assertEqual(subprocess_calls[0], 0,
                         "No subprocess must be spawned when circuit is open")

        circuit_open_events = [
            e for e in events
            if e.get("type") == "agent_state" and e.get("state") == "circuit_open"
        ]
        self.assertTrue(circuit_open_events, "circuit_open agent_state must be emitted")

    def test_breaker_trips_after_threshold(self):
        """After failure_threshold consecutive failures, is_open returns True."""
        breaker = relay.CircuitBreaker(failure_threshold=2, reset_timeout=9999.0)
        self.assertFalse(breaker.is_open("GEMINI"))
        breaker.record_failure("GEMINI")
        self.assertFalse(breaker.is_open("GEMINI"))
        breaker.record_failure("GEMINI")
        self.assertTrue(breaker.is_open("GEMINI"))

    def test_breaker_resets_on_success(self):
        breaker = relay.CircuitBreaker(failure_threshold=2, reset_timeout=9999.0)
        breaker.record_failure("GEMINI")
        breaker.record_failure("GEMINI")
        self.assertTrue(breaker.is_open("GEMINI"))
        breaker.record_success("GEMINI")
        self.assertFalse(breaker.is_open("GEMINI"))


if __name__ == "__main__":
    unittest.main()
