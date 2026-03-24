import asyncio
import urllib.error
import unittest
from unittest.mock import AsyncMock, patch

import dispatcher


class DispatcherTests(unittest.TestCase):
    def test_classify_message_parses_code_fenced_json(self):
        response = """```json
{"action":"route","targets":["claude"],"task_type":"code","priority":"high","reply":null}
```"""
        with patch("dispatcher.ollama_chat", new=AsyncMock(return_value=response)):
            result = asyncio.run(dispatcher.classify_message("fix it", "context", {}))

        self.assertEqual(result["action"], "route")
        self.assertEqual(result["targets"], ["CLAUDE"])
        self.assertEqual(result["task_type"], "code")
        self.assertEqual(result["priority"], "high")

    def test_health_check_reports_unavailable_on_error(self):
        with patch("dispatcher._ollama_get", side_effect=OSError("offline")):
            result = asyncio.run(dispatcher.health_check())

        self.assertEqual(result, {"available": False, "models": []})

    def test_classify_message_retries_on_transient_error(self):
        """Transient errors (URLError) should be retried before falling back."""
        call_count = 0
        valid_json = '{"action":"route","targets":["CLAUDE"],"task_type":"code","priority":"high","reply":null}'

        async def flaky_chat(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.URLError("connection refused")
            return valid_json

        with patch("dispatcher.ollama_chat", side_effect=flaky_chat):
            result = asyncio.run(
                dispatcher.classify_message("fix it", "context", {"router_retries": 2})
            )

        self.assertEqual(call_count, 2)
        self.assertEqual(result["action"], "route")
        self.assertEqual(result["targets"], ["CLAUDE"])
        self.assertNotIn("fallback", result)

    def test_classify_message_fallback_after_exhausted_retries(self):
        """After exhausting retries, should return fallback with flag."""
        mock_chat = AsyncMock(side_effect=urllib.error.URLError("connection refused"))

        with patch("dispatcher.ollama_chat", mock_chat):
            result = asyncio.run(
                dispatcher.classify_message("fix it", "ctx", {"router_retries": 1})
            )

        # 1 initial + 1 retry = 2 calls
        self.assertEqual(mock_chat.call_count, 2)
        self.assertTrue(result["fallback"])
        self.assertEqual(result["targets"], ["CLAUDE", "CODEX", "GEMINI"])

    def test_classify_message_no_retry_on_json_error(self):
        """JSONDecodeError is not transient — should not retry."""
        mock_chat = AsyncMock(return_value="not json at all {{{")

        with patch("dispatcher.ollama_chat", mock_chat):
            result = asyncio.run(
                dispatcher.classify_message("fix it", "ctx", {"router_retries": 2})
            )

        # Should only call once — no retries for parse errors
        self.assertEqual(mock_chat.call_count, 1)
        self.assertTrue(result["fallback"])


if __name__ == "__main__":
    unittest.main()
