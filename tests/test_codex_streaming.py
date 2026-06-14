"""
Tests for S3b: Codex token streaming via codex exec --json adapter.

Covers:
  1. parse_codex_stream_delta unit tests — feed real Codex --json NDJSON lines
     and assert correct text extraction and None for non-text event types.
  2. parse_codex_stream_final unit tests — consolidated text from full output.
  3. extract_session_id_from_codex_stream_json — thread_id extraction.
  4. Off-path regression — STREAM_TOKENS unset uses non-streaming code path
     with parse_codex_stream_final (JSON-aware parser replacing old parse_codex).
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import relay


# ─────────────────────────────────────────────────────────────────────────────
# 1. parse_codex_stream_delta
# ─────────────────────────────────────────────────────────────────────────────


class TestParseCodexStreamDelta(unittest.TestCase):
    """Verify delta extraction from real Codex --json NDJSON event shapes."""

    def _item_completed(self, text: str, item_type: str = "agent_message") -> str:
        return json.dumps({
            "type": "item.completed",
            "item": {"id": "item_0", "type": item_type, "text": text},
        })

    def test_returns_text_from_agent_message_item(self):
        line = self._item_completed("Hi there friend")
        self.assertEqual(relay.parse_codex_stream_delta(line), "Hi there friend")

    def test_returns_multiline_text(self):
        text = "Salt breath fills the dawn\nBlue waves fold the moonlight in\nDeep songs drift below"
        line = self._item_completed(text)
        self.assertEqual(relay.parse_codex_stream_delta(line), text)

    def test_returns_none_for_thread_started(self):
        line = json.dumps({"type": "thread.started", "thread_id": "019ec475-32cd-7290-88e6-32c72c165821"})
        self.assertIsNone(relay.parse_codex_stream_delta(line))

    def test_returns_none_for_turn_started(self):
        line = json.dumps({"type": "turn.started"})
        self.assertIsNone(relay.parse_codex_stream_delta(line))

    def test_returns_none_for_turn_completed(self):
        line = json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 11535, "cached_input_tokens": 4992, "output_tokens": 51},
        })
        self.assertIsNone(relay.parse_codex_stream_delta(line))

    def test_returns_none_for_non_agent_message_item(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "item_0", "type": "tool_call", "text": "ignored"},
        })
        self.assertIsNone(relay.parse_codex_stream_delta(line))

    def test_returns_none_for_empty_text(self):
        line = self._item_completed("")
        self.assertIsNone(relay.parse_codex_stream_delta(line))

    def test_returns_none_for_empty_line(self):
        self.assertIsNone(relay.parse_codex_stream_delta(""))

    def test_returns_none_for_malformed_json(self):
        self.assertIsNone(relay.parse_codex_stream_delta("not json"))

    def test_returns_none_for_whitespace_only_line(self):
        self.assertIsNone(relay.parse_codex_stream_delta("   "))

    def test_ordered_deltas_from_multiple_items(self):
        """Multiple item.completed events produce ordered delta sequence."""
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "019ec475-0000-0000-0000-000000000001"}),
            json.dumps({"type": "turn.started"}),
            self._item_completed("Hello"),
            self._item_completed(" world"),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        deltas = [relay.parse_codex_stream_delta(l) for l in lines]
        text_deltas = [d for d in deltas if d is not None]
        self.assertEqual(text_deltas, ["Hello", " world"])
        self.assertEqual("".join(text_deltas), "Hello world")


# ─────────────────────────────────────────────────────────────────────────────
# 2. parse_codex_stream_final
# ─────────────────────────────────────────────────────────────────────────────


class TestParseCodexStreamFinal(unittest.TestCase):
    """Verify consolidated reply extraction from full Codex --json NDJSON output."""

    def _make_raw(self, reply: str = "Hi there friend") -> str:
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "019ec475-32cd-7290-88e6-32c72c165821"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": reply}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11535, "output_tokens": 51}}),
        ]
        return "\n".join(lines)

    def test_extracts_single_agent_message(self):
        raw = self._make_raw("Hi there friend")
        self.assertEqual(relay.parse_codex_stream_final(raw), "Hi there friend")

    def test_concatenates_multiple_agent_messages(self):
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "019ec475-0000-0000-0000-000000000002"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Part one"}}),
            json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": " part two"}}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
        raw = "\n".join(lines)
        self.assertEqual(relay.parse_codex_stream_final(raw), "Part one part two")

    def test_skips_non_agent_message_items(self):
        lines = [
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "tool_call", "text": "ignored"}}),
            json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "real reply"}}),
        ]
        raw = "\n".join(lines)
        self.assertEqual(relay.parse_codex_stream_final(raw), "real reply")

    def test_empty_raw_returns_empty_string(self):
        self.assertEqual(relay.parse_codex_stream_final(""), "")

    def test_ignores_malformed_lines(self):
        lines = [
            "not json",
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "ok"}}),
        ]
        raw = "\n".join(lines)
        self.assertEqual(relay.parse_codex_stream_final(raw), "ok")

    def test_real_codex_json_output(self):
        """Feed the exact event stream from a real 'say hi in 3 words' run."""
        raw = (
            '{"type":"thread.started","thread_id":"019ec474-e6a5-73c3-a5e4-155c6de57161"}\n'
            '{"type":"turn.started"}\n'
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Hi there friend"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":11535,"cached_input_tokens":4992,"output_tokens":51,"reasoning_output_tokens":42}}'
        )
        self.assertEqual(relay.parse_codex_stream_final(raw), "Hi there friend")


# ─────────────────────────────────────────────────────────────────────────────
# 3. extract_session_id_from_codex_stream_json
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractSessionIdFromCodexStreamJson(unittest.TestCase):
    """Verify thread_id extraction from Codex --json NDJSON output."""

    def test_extracts_thread_id_from_thread_started(self):
        raw = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "019ec474-e6a5-73c3-a5e4-155c6de57161"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Hi"}}),
        ])
        result = relay.extract_session_id_from_codex_stream_json(raw, None)
        self.assertEqual(result, "019ec474-e6a5-73c3-a5e4-155c6de57161")

    def test_returns_fallback_when_no_thread_started(self):
        raw = "\n".join([
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Hi"}}),
        ])
        self.assertEqual(relay.extract_session_id_from_codex_stream_json(raw, "fallback-id"), "fallback-id")

    def test_returns_none_fallback_when_empty_raw(self):
        self.assertIsNone(relay.extract_session_id_from_codex_stream_json("", None))

    def test_ignores_empty_thread_id(self):
        raw = json.dumps({"type": "thread.started", "thread_id": ""})
        self.assertEqual(relay.extract_session_id_from_codex_stream_json(raw, "fb"), "fb")

    def test_returns_first_thread_id_when_multiple(self):
        raw = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "first-id-0000-0000-000000000001"}),
            json.dumps({"type": "thread.started", "thread_id": "second-id-000-0000-000000000002"}),
        ])
        self.assertEqual(relay.extract_session_id_from_codex_stream_json(raw, None), "first-id-0000-0000-000000000001")

    def test_real_codex_thread_id_format(self):
        """Thread IDs from real Codex --json output use the 019e* time-based UUID format."""
        raw = '{"type":"thread.started","thread_id":"019ec475-32cd-7290-88e6-32c72c165821"}\n{"type":"turn.started"}'
        result = relay.extract_session_id_from_codex_stream_json(raw, None)
        self.assertEqual(result, "019ec475-32cd-7290-88e6-32c72c165821")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Off-path regression — STREAM_TOKENS unset
# ─────────────────────────────────────────────────────────────────────────────


class TestCodexOffPathRegression(unittest.TestCase):
    """When STREAM_TOKENS is not set, Codex dispatch uses _exec_agent (non-streaming).

    The non-streaming parser is parse_codex_stream_final (JSON-aware), which
    produces the same text as the old parse_codex did for well-formed output.
    Verifies that token_callback is never invoked and _exec_agent_streaming is
    never called when STREAM_TOKENS is off.
    """

    def setUp(self):
        # Ensure STREAM_TOKENS is OFF for this test class
        self._original = relay.STREAM_TOKENS
        relay.STREAM_TOKENS = False

    def tearDown(self):
        relay.STREAM_TOKENS = self._original

    def test_codex_parser_in_parsers_dict_is_json_aware(self):
        """PARSERS['CODEX'] can parse --json NDJSON output correctly."""
        raw = (
            '{"type":"thread.started","thread_id":"019ec474-e6a5-73c3-a5e4-155c6de57161"}\n'
            '{"type":"turn.started"}\n'
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Hello from Codex"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":10}}'
        )
        parser = relay.PARSERS.get("CODEX")
        self.assertIsNotNone(parser)
        self.assertEqual(parser(raw), "Hello from Codex")

    def test_extract_session_id_uses_codex_json_path(self):
        """extract_session_id dispatches to JSON extractor for CODEX agent."""
        raw = '{"type":"thread.started","thread_id":"019ec474-dead-beef-0000-000000000042"}\n{"type":"turn.started"}'
        agent = {"name": "CODEX"}
        result = relay.extract_session_id(agent, raw, "", None)
        self.assertEqual(result, "019ec474-dead-beef-0000-000000000042")

    def test_non_streaming_path_does_not_invoke_streaming(self):
        """With STREAM_TOKENS=False, call_agent must not call _exec_agent_streaming."""
        import asyncio

        fake_json_stdout = (
            b'{"type":"thread.started","thread_id":"019ec474-0000-0000-0000-000000000099"}\n'
            b'{"type":"turn.started"}\n'
            b'{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"hi"}}\n'
            b'{"type":"turn.completed","usage":{}}'
        )

        agent = {
            "name": "CODEX",
            "cmd": "codex",
            "args": ["exec", "--json", "--skip-git-repo-check", "-C", "{script_dir}"],
            "invoke_resume_args": [],
            "work_dir": "/tmp",
            "timeout": 10,
            "io_log_path": None,
            "mirror_mode": "log",
            "preseed_session_id": False,
            "model_arg": [],
            "effort_arg": [],
            "model_options": [],
            "effort_options": [],
        }

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            sessions_path = Path(tmp) / "sessions.json"
            session_lock = asyncio.Lock()

            streaming_called = []

            async def fake_exec_agent(a, c, e):
                return fake_json_stdout, b"", 0

            async def fake_exec_agent_streaming(a, c, e, line_callback=None):
                streaming_called.append(True)
                return fake_json_stdout, b"", 0

            with (
                mock.patch.object(relay, "_exec_agent", side_effect=fake_exec_agent),
                mock.patch.object(relay, "_exec_agent_streaming", side_effect=fake_exec_agent_streaming),
                mock.patch.object(relay, "log_agent_io", return_value=None),
            ):
                result = asyncio.get_event_loop().run_until_complete(
                    relay.call_agent(agent, "say hi", sessions_path, session_lock, token_callback=None)
                )

            # _exec_agent_streaming must not have been called
            self.assertEqual(streaming_called, [], "_exec_agent_streaming must not be called when STREAM_TOKENS is off")
            # The reply should be correctly parsed from JSON
            self.assertEqual(result.reply, "hi")
            # Session ID should be extracted from thread.started
            self.assertEqual(result.session_id, "019ec474-0000-0000-0000-000000000099")

    def test_stream_tokens_on_uses_streaming_for_codex(self):
        """With STREAM_TOKENS=True, call_agent routes CODEX through _exec_agent_streaming."""
        import asyncio
        from pathlib import Path
        import tempfile

        relay.STREAM_TOKENS = True  # temporarily enable for this test

        fake_json_stdout = (
            b'{"type":"thread.started","thread_id":"019ec474-0000-0000-0000-000000000001"}\n'
            b'{"type":"turn.started"}\n'
            b'{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"streaming reply"}}\n'
            b'{"type":"turn.completed","usage":{}}'
        )

        agent = {
            "name": "CODEX",
            "cmd": "codex",
            "args": ["exec", "--json", "--skip-git-repo-check", "-C", "{script_dir}"],
            "invoke_resume_args": [],
            "work_dir": "/tmp",
            "timeout": 10,
            "io_log_path": None,
            "mirror_mode": "log",
            "preseed_session_id": False,
            "model_arg": [],
            "effort_arg": [],
            "model_options": [],
            "effort_options": [],
        }

        with tempfile.TemporaryDirectory() as tmp:
            sessions_path = Path(tmp) / "sessions.json"
            session_lock = asyncio.Lock()

            streaming_called = []
            received_deltas: list[str] = []

            async def fake_exec_agent_streaming(a, c, e, line_callback=None):
                streaming_called.append(True)
                # Simulate the streaming callback for each line
                if line_callback is not None:
                    for line in fake_json_stdout.decode().splitlines():
                        line_callback(line)
                return fake_json_stdout, b"", 0

            def token_cb(delta: str) -> None:
                received_deltas.append(delta)

            with (
                mock.patch.object(relay, "_exec_agent_streaming", side_effect=fake_exec_agent_streaming),
                mock.patch.object(relay, "log_agent_io", return_value=None),
            ):
                result = asyncio.get_event_loop().run_until_complete(
                    relay.call_agent(agent, "say hi", sessions_path, session_lock, token_callback=token_cb)
                )

            self.assertEqual(streaming_called, [True], "_exec_agent_streaming must be called when STREAM_TOKENS is on")
            self.assertEqual(received_deltas, ["streaming reply"])
            self.assertEqual(result.reply, "streaming reply")


if __name__ == "__main__":
    unittest.main()
