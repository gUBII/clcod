import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import relay


class RelayTests(unittest.TestCase):
    def test_last_speaker_supports_non_uppercase_tags(self):
        text = "[FARHAN]\nhello\n[Codex]\nreply\n"
        self.assertEqual(relay.last_speaker(text), "Codex")

    def test_parse_codex_extracts_response_block(self):
        raw = "\n".join(
            [
                "OpenAI Codex",
                "user",
                "prompt",
                "thinking",
                "stuff",
                "codex",
                "This is the answer.",
                "tokens used",
                "123",
                "This is repeated.",
            ]
        )
        self.assertEqual(relay.parse_codex(raw), "This is the answer.")

    def test_parse_gemini_strips_cached_credentials_banner(self):
        raw = "Loaded cached credentials.\nHello there."
        self.assertEqual(relay.parse_gemini(raw), "Hello there.")

    def test_activity_jitter_changes_with_message_volume(self):
        quiet = "[USER]\nhello\n"
        busy = "\n".join(["[A]", "[B]", "[C]", "[D]", "[E]", "[F]"])
        self.assertEqual(relay.activity_jitter(quiet), 2.0)
        self.assertEqual(relay.activity_jitter(busy), 0.5)

    def test_acquire_lock_reuses_stale_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "speaker.lock"
            lock_path.write_text("old-lock\n", encoding="utf-8")
            stale_time = time.time() - 120
            os.utime(lock_path, (stale_time, stale_time))

            self.assertTrue(relay.acquire_lock(lock_path, "relay:test", 90))
            relay.release_lock(lock_path)
            self.assertFalse(lock_path.exists())

    def test_load_config_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "name": "CLAUDE",
                                "cmd": "claude",
                                "args": "-p",
                            }
                        ],
                        "workspace": {
                            "log_path": "logs/room.txt",
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = relay.load_config(config_path)
            expected_log_path = (config_path.parent / "logs" / "room.txt").resolve()
            self.assertEqual(config["workspace"]["log_path"], expected_log_path)
            self.assertEqual(config["agents"][0]["args"], ["-p"])


if __name__ == "__main__":
    unittest.main()
