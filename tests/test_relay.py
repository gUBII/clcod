import json
import os
import tempfile
import time
import unittest
import uuid
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
                                "preseed_session_id": "seed-1",
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
            self.assertEqual(config["agents"][0]["preseed_session_id"], "seed-1")

    def test_build_agent_command_preseeds_session_id_for_resume_agents(self):
        agent = {
            "name": "CLAUDE",
            "cmd": "claude",
            "args": ["-p"],
            "invoke_resume_args": ["-p", "--session-id", "{session_id}"],
            "preseed_session_id": True,
        }

        cmd, session_id = relay.build_agent_command(agent, "hello", None)

        self.assertEqual(cmd[0], "claude")
        self.assertEqual(cmd[-1], "hello")
        self.assertEqual(cmd[1], "-p")
        self.assertEqual(cmd[2], "--session-id")
        uuid.UUID(session_id)

    def test_build_agent_command_uses_explicit_preseed_session_id(self):
        agent = {
            "name": "CLAUDE",
            "cmd": "claude",
            "args": ["-p"],
            "invoke_resume_args": ["-p", "--session-id", "{session_id}"],
            "preseed_session_id": "session-123",
        }

        cmd, session_id = relay.build_agent_command(agent, "hello", None)

        self.assertEqual(session_id, "session-123")
        self.assertEqual(cmd[2], "--session-id")
        self.assertEqual(cmd[3], "session-123")

    def test_seed_sessions_persists_preseeded_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_path = Path(tmpdir) / "sessions.json"
            relay.save_sessions(sessions_path, {})

            sessions = relay.seed_sessions(
                sessions_path,
                [
                    {
                        "name": "CLAUDE",
                        "enabled": True,
                        "preseed_session_id": "claude-seed",
                    },
                    {
                        "name": "CODEX",
                        "enabled": True,
                        "preseed_session_id": False,
                    },
                ],
            )

            self.assertEqual(sessions, {"CLAUDE": "claude-seed"})
            self.assertEqual(relay.load_sessions(sessions_path), {"CLAUDE": "claude-seed"})

    def test_extract_session_id_from_codex_stderr(self):
        agent = {"name": "CODEX"}
        stderr = "approval: never\nsession id: 123e4567-e89b-12d3-a456-426614174000\n"

        session_id = relay.extract_session_id(agent, "", stderr, None)

        self.assertEqual(session_id, "123e4567-e89b-12d3-a456-426614174000")


if __name__ == "__main__":
    unittest.main()
