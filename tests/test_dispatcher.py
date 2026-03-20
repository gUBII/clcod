import asyncio
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


if __name__ == "__main__":
    unittest.main()
