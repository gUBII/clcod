"""
Tests for S3a: Claude token streaming (relay→supervisor→app.js).

Covers:
  1. streaming-parser unit tests — verify delta extraction, final-reply extraction,
     and session_id extraction from Claude stream-json NDJSON.
  2. C2 replay-reconstruction test — token events must NOT be persisted; a
     Last-Event-ID reconnect must yield only the consolidated transcript message.
"""

from __future__ import annotations

import json
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import relay
import supervisor as sup_module
from event_store import EventStore


# ─────────────────────────────────────────────────────────────
# 1. Streaming-parser unit tests
# ─────────────────────────────────────────────────────────────


class TestParseCaudeStreamDelta(unittest.TestCase):
    def _line(self, obj: dict) -> str:
        return json.dumps(obj)

    def test_returns_text_from_assistant_chunk(self):
        line = self._line({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hello"}]},
        })
        self.assertEqual(relay.parse_claude_stream_delta(line), "Hello")

    def test_returns_none_for_result_line(self):
        line = self._line({"type": "result", "result": "Hello world", "session_id": "abc"})
        self.assertIsNone(relay.parse_claude_stream_delta(line))

    def test_returns_none_for_system_init_line(self):
        line = self._line({"type": "system", "subtype": "init", "session_id": "abc"})
        self.assertIsNone(relay.parse_claude_stream_delta(line))

    def test_returns_none_for_empty_content(self):
        line = self._line({"type": "assistant", "message": {"content": []}})
        self.assertIsNone(relay.parse_claude_stream_delta(line))

    def test_returns_none_for_non_text_content(self):
        line = self._line({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "x"}]},
        })
        self.assertIsNone(relay.parse_claude_stream_delta(line))

    def test_returns_none_for_malformed_json(self):
        self.assertIsNone(relay.parse_claude_stream_delta("not json"))

    def test_returns_none_for_empty_line(self):
        self.assertIsNone(relay.parse_claude_stream_delta(""))

    def test_handles_empty_text_field(self):
        line = self._line({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": ""}]},
        })
        self.assertIsNone(relay.parse_claude_stream_delta(line))

    def test_ordered_deltas_concatenate_correctly(self):
        lines = [
            self._line({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}),
            self._line({"type": "assistant", "message": {"content": [{"type": "text", "text": " "}]}}),
            self._line({"type": "assistant", "message": {"content": [{"type": "text", "text": "world"}]}}),
        ]
        result = "".join(
            d for d in (relay.parse_claude_stream_delta(l) for l in lines) if d is not None
        )
        self.assertEqual(result, "Hello world")


class TestParseCaudeStreamFinal(unittest.TestCase):
    def _make_raw(self, session_id: str = "ses-12345", result_text: str = "Hello world") -> str:
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": session_id}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": " world"}]}}),
            json.dumps({"type": "result", "result": result_text, "session_id": session_id}),
        ]
        return "\n".join(lines)

    def test_prefers_result_field(self):
        raw = self._make_raw(result_text="Hello world")
        self.assertEqual(relay.parse_claude_stream_final(raw), "Hello world")

    def test_falls_back_to_delta_concatenation_when_no_result_line(self):
        raw = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "A"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "B"}]}}),
        ])
        self.assertEqual(relay.parse_claude_stream_final(raw), "AB")

    def test_empty_raw_returns_empty_string(self):
        self.assertEqual(relay.parse_claude_stream_final(""), "")

    def test_ignores_malformed_lines(self):
        raw = "not json\n" + json.dumps({"type": "result", "result": "ok"})
        self.assertEqual(relay.parse_claude_stream_final(raw), "ok")


class TestExtractSessionIdFromStreamJson(unittest.TestCase):
    def test_extracts_from_system_init_line(self):
        raw = "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "abc12345"}),
            json.dumps({"type": "result", "result": "ok", "session_id": "abc12345"}),
        ])
        self.assertEqual(relay.extract_session_id_from_stream_json(raw, None), "abc12345")

    def test_extracts_from_result_line_only(self):
        raw = json.dumps({"type": "result", "result": "ok", "session_id": "xyz45678"})
        self.assertEqual(relay.extract_session_id_from_stream_json(raw, None), "xyz45678")

    def test_returns_fallback_when_no_session_id(self):
        raw = json.dumps({"type": "assistant", "message": {"content": []}})
        self.assertEqual(relay.extract_session_id_from_stream_json(raw, "fallback"), "fallback")

    def test_returns_none_fallback_when_no_session_id_and_no_fallback(self):
        self.assertIsNone(relay.extract_session_id_from_stream_json("", None))

    def test_ignores_empty_session_id(self):
        raw = json.dumps({"type": "result", "result": "ok", "session_id": ""})
        self.assertEqual(relay.extract_session_id_from_stream_json(raw, "fallback"), "fallback")


# ─────────────────────────────────────────────────────────────
# 2. C2 replay-reconstruction test
# ─────────────────────────────────────────────────────────────


def _make_minimal_supervisor(tmp_path: Path) -> sup_module.RuntimeSupervisor:
    """Build the minimal RuntimeSupervisor needed to exercise handle_relay_event."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "agents": [
            {
                "name": "CLAUDE",
                "enabled": True,
                "cmd": "claude",
                "args": ["-p", "--dangerously-skip-permissions"],
                "invoke_resume_args": [],
                "mirror_resume_args": [],
                "model_arg": [],
                "effort_arg": [],
                "model_options": [],
                "effort_options": [],
                "mirror_mode": "log",
                "preseed_session_id": False,
                "timeout": 60,
            }
        ],
        "workspace": {
            "log_path": str(tmp_path / "clcodgemmix.txt"),
            "lock_path": str(tmp_path / "speaker.lock"),
            "socket_path": str(tmp_path / "room.sock"),
            "relay_log_path": str(tmp_path / "relay.log"),
            "pid_path": str(tmp_path / "supervisor.pid"),
            "state_path": str(tmp_path / "state.json"),
            "sessions_path": str(tmp_path / "sessions.json"),
            "preferences_path": str(tmp_path / "preferences.json"),
            "projects_path": str(tmp_path / "projects.json"),
            "tasks_path": str(tmp_path / "tasks.json"),
            "events_db_path": str(tmp_path / "events.db"),
        },
        "locks": {"ttl": 90},
        "tmux": {"session": "triagent"},
        "ui": {
            "host": "127.0.0.1",
            "port": 4173,
            "password": "free",
            "default_sender": "Operator",
            "open_browser": False,
        },
        "dispatcher": {"enabled": False},
    }), encoding="utf-8")
    config = relay.load_config(config_path)
    return sup_module.RuntimeSupervisor(config)


class TestTokenEventsNotPersisted(unittest.TestCase):
    """C2 replay-reconstruction: token events are broadcast-only, not stored.

    When a client reconnects with Last-Event-ID it gets only persisted events.
    Token deltas must never appear in that replay — only the final consolidated
    transcript message may.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.sv = _make_minimal_supervisor(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_token_events_are_not_stored_in_event_store(self):
        ev_store = self.sv.event_store

        # Emit several streaming token events
        for seq, chunk in enumerate(["Hello", " world", "!"], start=1):
            self.sv.handle_relay_event({
                "type": "token",
                "agent": "CLAUDE",
                "delta": chunk,
                "seq": seq,
            })

        # Assert nothing was written to the event store
        stored = ev_store.list_events(after_id=0, limit=100)
        token_events = [e for e in stored if e.get("type") == "token"]
        self.assertEqual(
            len(token_events), 0,
            "Token events must not be persisted; found: %r" % token_events,
        )

    def test_consolidated_transcript_is_persisted_but_no_deltas(self):
        ev_store = self.sv.event_store

        # Stream several tokens — these must NOT reach the event_store
        for seq, chunk in enumerate(["Streaming ", "reply"], start=1):
            self.sv.handle_relay_event({
                "type": "token",
                "agent": "CLAUDE",
                "delta": chunk,
                "seq": seq,
            })

        # The consolidated transcript event is persisted via emit_local_event
        # (mirrors the production path: emit_event writes to store then calls callback)
        self.sv.emit_local_event({
            "type": "transcript",
            "last_speaker": "CLAUDE",
            "last_updated_at": "2026-01-01T00:00:00Z",
            "char_count": 14,
            "message": {
                "id": "msg-1",
                "sender": "CLAUDE",
                "body": "Streaming reply",
                "seq": 1,
                "ts": "2026-01-01T00:00:00Z",
                "type": "message",
            },
        })

        stored = ev_store.list_events(after_id=0, limit=100)
        token_events = [e for e in stored if e.get("type") == "token"]
        transcript_events = [e for e in stored if e.get("type") == "transcript"]

        self.assertEqual(len(token_events), 0, "Token deltas must not appear in replay")
        self.assertEqual(len(transcript_events), 1, "Consolidated transcript must be persisted exactly once")
        self.assertEqual(
            transcript_events[0].get("last_speaker"), "CLAUDE",
            "Stored transcript must carry the correct speaker",
        )

    def test_token_events_are_broadcast_to_sse_clients(self):
        """Token events reach SSE subscribers even though they are not persisted."""
        q = self.sv.sse_subscribe()
        self.assertIsNotNone(q)

        try:
            self.sv.handle_relay_event({
                "type": "token",
                "agent": "CLAUDE",
                "delta": "Hi",
                "seq": 1,
            })
            frame = q.get_nowait()
        finally:
            self.sv.sse_unsubscribe(q)

        payload = frame["payload"]
        self.assertEqual(payload["type"], "token")
        self.assertEqual(payload["agent"], "CLAUDE")
        self.assertEqual(payload["delta"], "Hi")
        self.assertEqual(payload["seq"], 1)
        # event_id must be None so the SSE frame carries no "id:" line
        self.assertIsNone(frame.get("event_id"))

    def test_replay_after_streaming_contains_no_token_events(self):
        """Reconnecting with Last-Event-ID yields only the consolidated transcript.

        In production, relay.py's emit_event persists the event to event_store
        THEN calls handle_relay_event.  Token events bypass emit_event entirely —
        they're emitted directly as event_callback({"type":"token",...}).
        This test simulates both paths to verify the invariant.
        """
        ev_store = self.sv.event_store

        # Stream tokens — go direct to handle_relay_event (bypass emit_event)
        for seq, chunk in enumerate(["A", "B", "C"], start=1):
            self.sv.handle_relay_event({"type": "token", "agent": "CLAUDE", "delta": chunk, "seq": seq})

        # Record high-water mark after token events (should still be 0)
        pre_id = ev_store.latest_event_id()
        self.assertEqual(pre_id, 0, "Token events must not advance the event_store cursor")

        # Consolidated transcript goes through emit_local_event (persist + handle)
        self.sv.emit_local_event({
            "type": "transcript",
            "last_speaker": "CLAUDE",
            "last_updated_at": "2026-01-01T00:00:00Z",
            "char_count": 3,
            "message": {
                "id": "msg-2", "sender": "CLAUDE", "body": "ABC",
                "seq": 2, "ts": "2026-01-01T00:00:00Z", "type": "message",
            },
        })

        # A client reconnecting with Last-Event-ID=0 gets everything after 0
        replay = ev_store.list_events(after_id=pre_id, limit=100)
        token_in_replay = [e for e in replay if e.get("type") == "token"]
        self.assertEqual(len(token_in_replay), 0, "Replay must contain zero token events")

        transcript_in_replay = [e for e in replay if e.get("type") == "transcript"]
        self.assertEqual(len(transcript_in_replay), 1)
        self.assertEqual(transcript_in_replay[0]["last_speaker"], "CLAUDE")


if __name__ == "__main__":
    unittest.main()
