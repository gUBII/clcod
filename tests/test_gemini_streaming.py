"""
Tests for S3c: Gemini token streaming via gemini -o stream-json adapter.

Covers:
  1. parse_gemini_stream_delta unit tests — feed real Gemini -o stream-json NDJSON
     lines and assert correct text extraction and None for non-text event types.
  2. parse_gemini_stream_final unit tests — consolidated text from full output.
  3. extract_session_id_from_gemini_stream_json — session_id extraction incl.
     the fallback-when-absent case.
  4. Off-path regression — STREAM_TOKENS unset uses non-streaming code path
     (_exec_agent + parse_gemini_stream_final); STREAM_TOKENS on routes through
     _exec_agent_streaming for GEMINI.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import relay


# ─────────────────────────────────────────────────────────────────────────────
# 1. parse_gemini_stream_delta
# ─────────────────────────────────────────────────────────────────────────────


class TestParseGeminiStreamDelta(unittest.TestCase):
    """Verify delta extraction from real Gemini -o stream-json NDJSON event shapes."""

    def _assistant_delta(self, content: str) -> str:
        return json.dumps({
            "type": "message",
            "role": "assistant",
            "content": content,
            "delta": True,
        })

    def test_returns_content_from_assistant_delta(self):
        line = self._assistant_delta("Hi there friend.")
        self.assertEqual(relay.parse_gemini_stream_delta(line), "Hi there friend.")

    def test_returns_multiline_content(self):
        text = "Salt breath fills the dawn\nBlue waves fold the moonlight in\nDeep songs drift below"
        line = self._assistant_delta(text)
        self.assertEqual(relay.parse_gemini_stream_delta(line), text)

    def test_returns_none_for_init_line(self):
        line = json.dumps({
            "type": "init",
            "session_id": "fea7accd-604b-43a2-afcd-ce6c051a7f19",
            "model": "gemini-3.1-pro-preview",
        })
        self.assertIsNone(relay.parse_gemini_stream_delta(line))

    def test_returns_none_for_user_message(self):
        line = json.dumps({
            "type": "message",
            "role": "user",
            "content": "say hi in 3 words",
        })
        self.assertIsNone(relay.parse_gemini_stream_delta(line))

    def test_returns_none_for_result_line(self):
        line = json.dumps({
            "type": "result",
            "status": "success",
            "stats": {"total_tokens": 10527, "output_tokens": 5},
        })
        self.assertIsNone(relay.parse_gemini_stream_delta(line))

    def test_returns_none_for_assistant_without_delta_flag(self):
        line = json.dumps({
            "type": "message",
            "role": "assistant",
            "content": "no delta flag here",
        })
        self.assertIsNone(relay.parse_gemini_stream_delta(line))

    def test_returns_none_for_assistant_delta_false(self):
        line = json.dumps({
            "type": "message",
            "role": "assistant",
            "content": "delta is false",
            "delta": False,
        })
        self.assertIsNone(relay.parse_gemini_stream_delta(line))

    def test_returns_none_for_empty_content(self):
        line = self._assistant_delta("")
        self.assertIsNone(relay.parse_gemini_stream_delta(line))

    def test_returns_none_for_empty_line(self):
        self.assertIsNone(relay.parse_gemini_stream_delta(""))

    def test_returns_none_for_malformed_json(self):
        self.assertIsNone(relay.parse_gemini_stream_delta("not json"))

    def test_returns_none_for_whitespace_only_line(self):
        self.assertIsNone(relay.parse_gemini_stream_delta("   "))

    def test_ordered_deltas_from_multiple_chunks(self):
        """Multiple assistant delta events produce ordered delta sequence."""
        lines = [
            json.dumps({"type": "init", "session_id": "abc-123", "model": "gemini-3.1-pro-preview"}),
            json.dumps({"type": "message", "role": "user", "content": "say hi"}),
            self._assistant_delta("Hello"),
            self._assistant_delta(" world"),
            json.dumps({"type": "result", "status": "success", "stats": {}}),
        ]
        deltas = [relay.parse_gemini_stream_delta(l) for l in lines]
        text_deltas = [d for d in deltas if d is not None]
        self.assertEqual(text_deltas, ["Hello", " world"])
        self.assertEqual("".join(text_deltas), "Hello world")


# ─────────────────────────────────────────────────────────────────────────────
# 2. parse_gemini_stream_final
# ─────────────────────────────────────────────────────────────────────────────


class TestParseGeminiStreamFinal(unittest.TestCase):
    """Verify consolidated reply extraction from full Gemini -o stream-json NDJSON output."""

    def _make_raw(self, reply: str = "Hi there friend.") -> str:
        lines = [
            json.dumps({"type": "init", "timestamp": "2026-06-14T06:10:00.841Z", "session_id": "fea7accd-604b-43a2-afcd-ce6c051a7f19", "model": "gemini-3.1-pro-preview"}),
            json.dumps({"type": "message", "role": "user", "content": "say hi in 3 words"}),
            json.dumps({"type": "message", "role": "assistant", "content": reply, "delta": True}),
            json.dumps({"type": "result", "status": "success", "stats": {"total_tokens": 10527, "output_tokens": 5}}),
        ]
        return "\n".join(lines)

    def test_extracts_single_assistant_message(self):
        raw = self._make_raw("Hi there friend.")
        self.assertEqual(relay.parse_gemini_stream_final(raw), "Hi there friend.")

    def test_concatenates_multiple_assistant_deltas(self):
        lines = [
            json.dumps({"type": "init", "session_id": "abc-0001", "model": "gemini-2.5-pro"}),
            json.dumps({"type": "message", "role": "user", "content": "say hi"}),
            json.dumps({"type": "message", "role": "assistant", "content": "Part one", "delta": True}),
            json.dumps({"type": "message", "role": "assistant", "content": " part two", "delta": True}),
            json.dumps({"type": "result", "status": "success", "stats": {}}),
        ]
        raw = "\n".join(lines)
        self.assertEqual(relay.parse_gemini_stream_final(raw), "Part one part two")

    def test_skips_non_delta_messages(self):
        lines = [
            json.dumps({"type": "message", "role": "assistant", "content": "not a delta"}),
            json.dumps({"type": "message", "role": "assistant", "content": "real delta", "delta": True}),
        ]
        raw = "\n".join(lines)
        self.assertEqual(relay.parse_gemini_stream_final(raw), "real delta")

    def test_skips_user_messages(self):
        lines = [
            json.dumps({"type": "message", "role": "user", "content": "user input"}),
            json.dumps({"type": "message", "role": "assistant", "content": "assistant reply", "delta": True}),
        ]
        raw = "\n".join(lines)
        self.assertEqual(relay.parse_gemini_stream_final(raw), "assistant reply")

    def test_empty_raw_returns_empty_string(self):
        self.assertEqual(relay.parse_gemini_stream_final(""), "")

    def test_ignores_malformed_lines(self):
        lines = [
            "not json",
            json.dumps({"type": "message", "role": "assistant", "content": "ok", "delta": True}),
        ]
        raw = "\n".join(lines)
        self.assertEqual(relay.parse_gemini_stream_final(raw), "ok")

    def test_real_gemini_stream_json_output(self):
        """Feed the exact event stream from a real 'say hi in 3 words' run."""
        raw = (
            '{"type":"init","timestamp":"2026-06-14T06:10:00.841Z","session_id":"fea7accd-604b-43a2-afcd-ce6c051a7f19","model":"gemini-3.1-pro-preview"}\n'
            '{"type":"message","timestamp":"2026-06-14T06:10:00.843Z","role":"user","content":"say hi in 3 words"}\n'
            '{"type":"message","timestamp":"2026-06-14T06:10:05.237Z","role":"assistant","content":"Hi there, user.","delta":true}\n'
            '{"type":"result","timestamp":"2026-06-14T06:10:05.303Z","status":"success","stats":{"total_tokens":10527,"input_tokens":10332,"output_tokens":5,"cached":0}}'
        )
        self.assertEqual(relay.parse_gemini_stream_final(raw), "Hi there, user.")


# ─────────────────────────────────────────────────────────────────────────────
# 3. extract_session_id_from_gemini_stream_json
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractSessionIdFromGeminiStreamJson(unittest.TestCase):
    """Verify session_id extraction from Gemini -o stream-json NDJSON output."""

    def test_extracts_session_id_from_init_line(self):
        raw = "\n".join([
            json.dumps({"type": "init", "session_id": "fea7accd-604b-43a2-afcd-ce6c051a7f19", "model": "gemini-3.1-pro-preview"}),
            json.dumps({"type": "message", "role": "user", "content": "hi"}),
            json.dumps({"type": "message", "role": "assistant", "content": "Hi!", "delta": True}),
        ])
        result = relay.extract_session_id_from_gemini_stream_json(raw, None)
        self.assertEqual(result, "fea7accd-604b-43a2-afcd-ce6c051a7f19")

    def test_returns_fallback_when_no_init_line(self):
        raw = "\n".join([
            json.dumps({"type": "message", "role": "user", "content": "hi"}),
            json.dumps({"type": "message", "role": "assistant", "content": "Hi!", "delta": True}),
        ])
        self.assertEqual(relay.extract_session_id_from_gemini_stream_json(raw, "fallback-id"), "fallback-id")

    def test_returns_none_fallback_when_empty_raw(self):
        self.assertIsNone(relay.extract_session_id_from_gemini_stream_json("", None))

    def test_returns_fallback_when_init_has_empty_session_id(self):
        raw = json.dumps({"type": "init", "session_id": "", "model": "gemini-2.5-flash"})
        self.assertEqual(relay.extract_session_id_from_gemini_stream_json(raw, "fb"), "fb")

    def test_returns_fallback_when_init_missing_session_id_field(self):
        raw = json.dumps({"type": "init", "model": "gemini-2.5-flash"})
        self.assertIsNone(relay.extract_session_id_from_gemini_stream_json(raw, None))

    def test_returns_first_session_id_when_multiple_init_lines(self):
        raw = "\n".join([
            json.dumps({"type": "init", "session_id": "first-id-000-0000-000000000001"}),
            json.dumps({"type": "init", "session_id": "second-id-00-0000-000000000002"}),
        ])
        self.assertEqual(relay.extract_session_id_from_gemini_stream_json(raw, None), "first-id-000-0000-000000000001")

    def test_real_gemini_session_id_format(self):
        """Session IDs from real Gemini -o stream-json use standard UUID format."""
        raw = '{"type":"init","timestamp":"2026-06-14T06:10:00.841Z","session_id":"fea7accd-604b-43a2-afcd-ce6c051a7f19","model":"gemini-3.1-pro-preview"}'
        result = relay.extract_session_id_from_gemini_stream_json(raw, None)
        self.assertEqual(result, "fea7accd-604b-43a2-afcd-ce6c051a7f19")

    def test_ignores_malformed_lines(self):
        raw = "\n".join([
            "not json",
            json.dumps({"type": "init", "session_id": "valid-id-00-0000-000000000042"}),
        ])
        result = relay.extract_session_id_from_gemini_stream_json(raw, None)
        self.assertEqual(result, "valid-id-00-0000-000000000042")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Off-path regression — STREAM_TOKENS unset / set
# ─────────────────────────────────────────────────────────────────────────────


class TestGeminiOffPathRegression(unittest.TestCase):
    """When STREAM_TOKENS is not set, Gemini dispatch uses _exec_agent (non-streaming).

    Verifies that token_callback is never invoked and _exec_agent_streaming is
    never called when STREAM_TOKENS is off, and that the reply is correctly
    parsed from stream-json NDJSON output.
    """

    def setUp(self):
        self._original = relay.STREAM_TOKENS
        relay.STREAM_TOKENS = False

    def tearDown(self):
        relay.STREAM_TOKENS = self._original

    def test_gemini_parser_in_parsers_dict_is_stream_json_aware(self):
        """PARSERS['GEMINI'] can parse -o stream-json NDJSON output correctly."""
        raw = (
            '{"type":"init","session_id":"fea7accd-604b-43a2-afcd-ce6c051a7f19","model":"gemini-3.1-pro-preview"}\n'
            '{"type":"message","role":"user","content":"say hi"}\n'
            '{"type":"message","role":"assistant","content":"Hello from Gemini","delta":true}\n'
            '{"type":"result","status":"success","stats":{}}'
        )
        parser = relay.PARSERS.get("GEMINI")
        self.assertIsNotNone(parser)
        self.assertEqual(parser(raw), "Hello from Gemini")

    def test_extract_session_id_uses_gemini_json_path(self):
        """extract_session_id dispatches to JSON extractor for GEMINI agent."""
        raw = '{"type":"init","session_id":"fea7accd-dead-beef-0000-000000000042"}'
        agent = {"name": "GEMINI"}
        result = relay.extract_session_id(agent, raw, "", None)
        self.assertEqual(result, "fea7accd-dead-beef-0000-000000000042")

    def test_extract_session_id_returns_fallback_when_no_init(self):
        """extract_session_id falls back gracefully when init line is absent."""
        raw = '{"type":"message","role":"assistant","content":"hi","delta":true}'
        agent = {"name": "GEMINI"}
        result = relay.extract_session_id(agent, raw, "", "stored-session-id")
        self.assertEqual(result, "stored-session-id")

    def test_non_streaming_path_does_not_invoke_streaming(self):
        """With STREAM_TOKENS=False, call_agent must not call _exec_agent_streaming for GEMINI."""
        import asyncio
        import tempfile
        from pathlib import Path

        fake_stream_json_stdout = (
            b'{"type":"init","session_id":"fea7accd-0000-0000-0000-000000000099","model":"gemini-3.1-pro-preview"}\n'
            b'{"type":"message","role":"user","content":"say hi"}\n'
            b'{"type":"message","role":"assistant","content":"hi","delta":true}\n'
            b'{"type":"result","status":"success","stats":{}}'
        )

        agent = {
            "name": "GEMINI",
            "cmd": "gemini",
            "args": ["-y", "-o", "stream-json", "-p"],
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

            async def fake_exec_agent(a, c, e):
                return fake_stream_json_stdout, b"", 0

            async def fake_exec_agent_streaming(a, c, e, line_callback=None):
                streaming_called.append(True)
                return fake_stream_json_stdout, b"", 0

            with (
                mock.patch.object(relay, "_exec_agent", side_effect=fake_exec_agent),
                mock.patch.object(relay, "_exec_agent_streaming", side_effect=fake_exec_agent_streaming),
                mock.patch.object(relay, "log_agent_io", return_value=None),
            ):
                result = asyncio.run(
                    relay.call_agent(agent, "say hi", sessions_path, session_lock, token_callback=None)
                )

            self.assertEqual(streaming_called, [], "_exec_agent_streaming must not be called when STREAM_TOKENS is off")
            self.assertEqual(result.reply, "hi")
            self.assertEqual(result.session_id, "fea7accd-0000-0000-0000-000000000099")

    def test_stream_tokens_on_uses_streaming_for_gemini(self):
        """With STREAM_TOKENS=True, call_agent routes GEMINI through _exec_agent_streaming."""
        import asyncio
        import tempfile
        from pathlib import Path

        relay.STREAM_TOKENS = True

        fake_stream_json_stdout = (
            b'{"type":"init","session_id":"fea7accd-0000-0000-0000-000000000001","model":"gemini-3.1-pro-preview"}\n'
            b'{"type":"message","role":"user","content":"say hi"}\n'
            b'{"type":"message","role":"assistant","content":"streaming reply","delta":true}\n'
            b'{"type":"result","status":"success","stats":{}}'
        )

        agent = {
            "name": "GEMINI",
            "cmd": "gemini",
            "args": ["-y", "-o", "stream-json", "-p"],
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
                if line_callback is not None:
                    for line in fake_stream_json_stdout.decode().splitlines():
                        line_callback(line)
                return fake_stream_json_stdout, b"", 0

            def token_cb(delta: str) -> None:
                received_deltas.append(delta)

            with (
                mock.patch.object(relay, "_exec_agent_streaming", side_effect=fake_exec_agent_streaming),
                mock.patch.object(relay, "log_agent_io", return_value=None),
            ):
                result = asyncio.run(
                    relay.call_agent(agent, "say hi", sessions_path, session_lock, token_callback=token_cb)
                )

            self.assertEqual(streaming_called, [True], "_exec_agent_streaming must be called when STREAM_TOKENS is on")
            self.assertEqual(received_deltas, ["streaming reply"])
            self.assertEqual(result.reply, "streaming reply")
            self.assertEqual(result.session_id, "fea7accd-0000-0000-0000-000000000001")


if __name__ == "__main__":
    unittest.main()
