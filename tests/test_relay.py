import json
import os
import tempfile
import time
import unittest
import uuid
import asyncio
from pathlib import Path
from unittest import mock

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

    def test_release_lock_ignores_missing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "nonexistent.lock"
            # Should not raise
            relay.release_lock(lock_path)

    def test_release_lock_propagates_unexpected_oserror(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "speaker.lock"
            lock_path.write_text("lock\n", encoding="utf-8")

            with mock.patch.object(
                type(lock_path),
                "unlink",
                side_effect=PermissionError("operation not permitted"),
            ):
                with self.assertRaises(PermissionError):
                    relay.release_lock(lock_path)

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
            self.assertTrue(str(config["workspace"]["preferences_path"]).endswith("preferences.json"))
            self.assertTrue(str(config["workspace"]["projects_path"]).endswith("projects.json"))
            self.assertTrue(str(config["workspace"]["tasks_path"]).endswith("tasks.json"))
            self.assertEqual(config["agents"][0]["args"], ["-p"])
            self.assertEqual(config["agents"][0]["preseed_session_id"], "seed-1")

    def test_load_config_preserves_runtime_workdir_templates_in_agent_args(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "name": "CODEX",
                                "cmd": "codex",
                                "args": ["exec", "-C", "{script_dir}"],
                                "invoke_resume_args": [
                                    "exec",
                                    "--dangerously-bypass-approvals-and-sandbox",
                                    "-C",
                                    "{script_dir}",
                                    "resume",
                                    "{session_id}",
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config = relay.load_config(config_path)
            agent = config["agents"][0]

            self.assertEqual(agent["args"], ["exec", "-C", "{script_dir}"])
            self.assertEqual(
                agent["invoke_resume_args"],
                [
                    "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-C",
                    "{script_dir}",
                    "resume",
                    "{session_id}",
                ],
            )

            agent["work_dir"] = "/tmp/demo-project"
            cmd, _ = relay.build_agent_command(agent, "hello", None)
            resume_cmd, session_id = relay.build_agent_command(agent, "hello", "session-1")

            self.assertEqual(cmd, ["codex", "exec", "-C", "/tmp/demo-project", "hello"])
            self.assertEqual(session_id, "session-1")
            self.assertEqual(
                resume_cmd,
                [
                    "codex",
                    "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-C",
                    "/tmp/demo-project",
                    "resume",
                    "session-1",
                    "hello",
                ],
            )

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

    def test_build_agent_command_applies_selected_model_and_effort(self):
        agent = {
            "name": "CLAUDE",
            "cmd": "claude",
            "args": ["-p"],
            "invoke_resume_args": ["-p", "--session-id", "{session_id}"],
            "preseed_session_id": False,
            "model_arg": ["--model", "{value}"],
            "effort_arg": ["--effort", "{value}"],
            "model_options": [relay.build_option("default"), relay.build_option("sonnet")],
            "effort_options": [relay.build_option("default"), relay.build_option("high")],
            "selected_model": "sonnet",
            "selected_effort": "high",
        }

        cmd, session_id = relay.build_agent_command(agent, "hello", None)

        self.assertIsNone(session_id)
        self.assertEqual(
            cmd,
            ["claude", "--model", "sonnet", "--effort", "high", "-p", "hello"],
        )

    def test_build_agent_command_uses_work_dir_for_formatted_args(self):
        agent = {
            "name": "CODEX",
            "cmd": "codex",
            "args": ["exec", "-C", "{script_dir}"],
            "invoke_resume_args": ["exec", "resume", "-C", "{work_dir}", "{session_id}"],
            "preseed_session_id": False,
            "work_dir": "/tmp/demo-worktree",
        }

        cmd, session_id = relay.build_agent_command(agent, "hello", None)
        self.assertIsNone(session_id)
        self.assertEqual(cmd, ["codex", "exec", "-C", "/tmp/demo-worktree", "hello"])

        resume_cmd, resume_session_id = relay.build_agent_command(agent, "hello", "session-1")
        self.assertEqual(resume_session_id, "session-1")
        self.assertEqual(
            resume_cmd,
            ["codex", "exec", "resume", "-C", "/tmp/demo-worktree", "session-1", "hello"],
        )

    def test_extract_target_supports_slash_task_prefix(self):
        self.assertEqual(relay.extract_target("/task @CODEX Verify the plan"), "CODEX")
        self.assertEqual(relay.task_request_from_message("/task @CODEX Verify the plan"), "Verify the plan")

    def test_build_agent_prompt_uses_task_template_for_explicit_tasks(self):
        prompt = relay.build_agent_prompt(
            agent_name="CODEX",
            context="recent transcript",
            work_dir="/tmp/demo",
            task={
                "id": 8,
                "title": "Verify the Plan thoroughly",
                "request": "Verify ingress, persistence, and view wiring.",
            },
        )

        self.assertIn("This is an explicit task assignment, not casual chat.", prompt)
        self.assertIn("Task #8: Verify the Plan thoroughly", prompt)
        self.assertIn("Task request:\nVerify ingress, persistence, and view wiring.", prompt)

    def test_effective_effort_id_uses_model_safe_codex_default(self):
        agent = {
            "name": "CODEX",
            "selected_model": "gpt-5.1-codex-mini",
            "selected_effort": "default",
            "effort_matrix": {
                "gpt-5.1-codex-mini": ["default", "medium", "high"],
            },
        }

        self.assertEqual(relay.effective_effort_id(agent), "medium")

    def test_build_selection_args_uses_safe_codex_effort_when_default_selected(self):
        agent = {
            "name": "CODEX",
            "model_arg": ["-m", "{value}"],
            "effort_arg": ["-c", "model_reasoning_effort=\"{value}\""],
            "model_options": [relay.build_option("default"), relay.build_option("gpt-5.1-codex-mini")],
            "effort_options": [
                relay.build_option("default"),
                relay.build_option("medium"),
                relay.build_option("high"),
            ],
            "effort_matrix": {
                "gpt-5.1-codex-mini": ["default", "medium", "high"],
            },
            "selected_model": "gpt-5.1-codex-mini",
            "selected_effort": "default",
        }

        self.assertEqual(
            relay.build_selection_args(agent),
            ["-m", "gpt-5.1-codex-mini", "-c", 'model_reasoning_effort="medium"'],
        )

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

    def test_create_task_sets_assigned_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tasks_path = Path(tmpdir) / "tasks.json"
            task = relay.create_task(
                tasks_path,
                title="test task",
                assigned_to=["CLAUDE", "CODEX"],
            )
            self.assertEqual(task["status"], "assigned")

    def test_create_task_sets_pending_without_agents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tasks_path = Path(tmpdir) / "tasks.json"
            task = relay.create_task(
                tasks_path,
                title="unassigned task",
            )
            self.assertEqual(task["status"], "pending")

    def test_append_tagged_entry_returns_message_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "room.txt"

            message = relay.append_tagged_entry(log_path, "CODEX", "payload test")

            self.assertEqual(message["sender"], "CODEX")
            self.assertEqual(message["body"], "payload test")
            self.assertEqual(message["type"], "message")
            self.assertIsInstance(message["seq"], int)
            self.assertTrue(message["ts"])

            persisted = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(persisted["id"], message["id"])
            self.assertEqual(persisted["sender"], message["sender"])
            self.assertEqual(persisted["body"], message["body"])

    def test_route_to_emits_transcript_event_with_message_payload(self):
        async def run_case() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                log_path = Path(tmpdir) / "room.txt"
                sessions_path = Path(tmpdir) / "sessions.json"
                relay.save_sessions(sessions_path, {})
                events: list[dict[str, object]] = []

                with mock.patch(
                    "relay.call_agent",
                    new=mock.AsyncMock(
                        return_value=relay.AgentCallResult(
                            reply="Agent reply",
                            raw="",
                            stderr="",
                            session_id="session-1",
                        )
                    ),
                ):
                    await relay.route_to(
                        {"name": "CODEX"},
                        "prompt",
                        log_path,
                        asyncio.Lock(),
                        sessions_path,
                        asyncio.Lock(),
                        event_callback=events.append,
                    )

                self.assertEqual(events[0]["type"], "agent_state")
                self.assertEqual(events[0]["state"], "warming")

                transcript_event = events[1]
                self.assertEqual(transcript_event["type"], "transcript")
                self.assertEqual(transcript_event["last_speaker"], "CODEX")
                self.assertEqual(transcript_event["char_count"], len("Agent reply"))
                self.assertEqual(transcript_event["message"]["sender"], "CODEX")
                self.assertEqual(transcript_event["message"]["body"], "Agent reply")
                self.assertEqual(transcript_event["message"]["type"], "message")
                self.assertIsInstance(transcript_event["message"]["seq"], int)
                self.assertTrue(transcript_event["message"]["ts"])

                self.assertEqual(events[2]["type"], "agent_state")
                self.assertEqual(events[2]["state"], "ready")
                self.assertEqual(events[2]["session_id"], "session-1")

        asyncio.run(run_case())

    def test_route_to_emits_route_state_for_live_dispatch(self):
        async def run_case() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                log_path = Path(tmpdir) / "room.txt"
                sessions_path = Path(tmpdir) / "sessions.json"
                relay.save_sessions(sessions_path, {})
                events: list[dict[str, object]] = []
                route = {
                    "route_id": "route-1",
                    "task_id": 7,
                    "task_title": "Verify route bus",
                    "body_preview": "Verify the TXX/RXX lane end to end.",
                    "sender": "FARHAN",
                    "target": "CODEX",
                    "source": "dispatcher",
                    "message_kind": "task",
                    "started_at": "2026-03-22T05:00:00Z",
                }

                with mock.patch(
                    "relay.call_agent",
                    new=mock.AsyncMock(
                        return_value=relay.AgentCallResult(
                            reply="Done.",
                            raw="",
                            stderr="",
                            session_id="session-7",
                        )
                    ),
                ):
                    await relay.route_to(
                        {"name": "CODEX", "timeout": 180},
                        "prompt",
                        log_path,
                        asyncio.Lock(),
                        sessions_path,
                        asyncio.Lock(),
                        event_callback=events.append,
                        route=route,
                    )

                route_events = [event for event in events if event["type"] == "route_state"]
                self.assertEqual(len(route_events), 2)
                self.assertEqual(route_events[0]["status"], "transmitting")
                self.assertEqual(route_events[0]["tx_state"], "active")
                self.assertEqual(route_events[0]["rx_state"], "waiting")
                self.assertEqual(route_events[1]["status"], "complete")
                self.assertEqual(route_events[1]["tx_state"], "sent")
                self.assertEqual(route_events[1]["rx_state"], "received")
                self.assertEqual(route_events[1]["session_id"], "session-7")

        asyncio.run(run_case())

    def test_route_to_tolerates_agent_without_timeout(self):
        """route_to must not raise KeyError when agent dict has no 'timeout' key."""
        async def run_case() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                log_path = Path(tmpdir) / "room.txt"
                sessions_path = Path(tmpdir) / "sessions.json"
                relay.save_sessions(sessions_path, {})

                with mock.patch(
                    "relay.call_agent",
                    new=mock.AsyncMock(
                        return_value=relay.AgentCallResult(
                            reply="ok",
                            raw="",
                            stderr="",
                            session_id=None,
                        )
                    ),
                ):
                    # Agent dict has no "timeout" key — should use .get("timeout", 60) default
                    await relay.route_to(
                        {"name": "CLAUDE"},
                        "hello",
                        log_path,
                        asyncio.Lock(),
                        sessions_path,
                        asyncio.Lock(),
                    )

        asyncio.run(run_case())

    def test_session_id_validator_accepts_valid_and_rejects_garbage(self):
        """_SESSION_ID_RE accepts real-looking IDs and rejects garbage / overlong strings."""
        valid_ids = [
            "abcdefgh",                    # 8 chars, minimum
            "abc-def-012",                 # hyphens
            "A1b2C3d4",                    # mixed case
            "a" * 128,                     # maximum length
            "session-abc123.XYZ_ok",       # real-looking id with all allowed chars
        ]
        invalid_ids = [
            "",                            # empty
            "abc",                         # too short (< 8 chars)
            "a" * 129,                     # too long (> 128 chars)
            "has space here1234",          # space not allowed
            "has/slash1234567",            # slash not allowed
            "has@at12345678",              # @ not allowed
            "has\x00null1234",             # null byte
        ]
        for sid in valid_ids:
            self.assertIsNotNone(
                relay._SESSION_ID_RE.match(sid),
                f"Expected valid: {sid!r}",
            )
        for sid in invalid_ids:
            self.assertIsNone(
                relay._SESSION_ID_RE.match(sid),
                f"Expected invalid: {sid!r}",
            )

    def test_extract_session_id_rejects_garbage_and_accepts_real(self):
        """extract_session_id_from_stream_json falls back when session_id is garbage."""
        garbage_ndjson = json.dumps({"type": "system", "session_id": "!!bad!!"}) + "\n"
        result = relay.extract_session_id_from_stream_json(garbage_ndjson, "fallback-id")
        self.assertEqual(result, "fallback-id")

        valid_sid = "abc123defg"  # 10 chars, all alphanumeric
        good_ndjson = json.dumps({"type": "system", "session_id": valid_sid}) + "\n"
        result = relay.extract_session_id_from_stream_json(good_ndjson, "fallback-id")
        self.assertEqual(result, valid_sid)

    def test_streaming_retry_suppresses_token_callback(self):
        """On streaming retry, line_callback=None is passed so tokens are not double-emitted.

        Requires STREAM_TOKENS=True and a pre-existing session (so _was_resuming=True)
        to trigger the resume-failure cold-spawn fallback path.
        """
        async def run_case() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                sessions_path = Path(tmpdir) / "sessions.json"
                relay.save_sessions(sessions_path, {"CLAUDE": "old-session-id"})

                agent = {
                    "name": "CLAUDE",
                    "cmd": "claude",
                    "args": ["-p", "--dangerously-skip-permissions"],
                    "invoke_resume_args": ["-p", "--dangerously-skip-permissions", "--session-id", "{session_id}"],
                    "timeout": 10,
                    "io_log_path": None,
                    "work_dir": None,
                    "preseed_session_id": True,
                }
                call_count = 0
                captured_callbacks: list = []

                async def fake_streaming(ag, cmd, env, line_callback=None):
                    nonlocal call_count
                    call_count += 1
                    captured_callbacks.append(line_callback)
                    if call_count == 1:
                        # Resume attempt fails (non-session-in-use error) → triggers cold-spawn retry
                        return b"", b"fatal error", 1
                    # Second call: cold-spawn succeeds
                    line = json.dumps({
                        "type": "result", "result": "hello", "session_id": "abcdefghij"
                    }) + "\n"
                    return line.encode(), b"", 0

                with mock.patch("relay._exec_agent_streaming", side_effect=fake_streaming), \
                     mock.patch("relay.log_agent_io"), \
                     mock.patch("relay.STREAM_TOKENS", True):
                    await relay.call_agent(
                        agent, "test prompt",
                        sessions_path,
                        asyncio.Lock(),
                        token_callback=lambda d: None,
                    )

            # First call (resume): may have streaming callback
            # Second call (cold-spawn retry): MUST have line_callback=None to prevent duplication
            self.assertEqual(call_count, 2)
            self.assertIsNone(captured_callbacks[1], "retry attempt must pass line_callback=None")

        asyncio.run(run_case())

    def test_scrub_claude_stream_json_removes_tool_result(self):
        """Test that tool_result events are scrubbed to metadata-only."""
        raw = json.dumps({"type": "tool_result", "id": "123", "content": "secret file contents"})
        scrubbed = relay._scrub_claude_stream_json(raw)
        obj = json.loads(scrubbed)
        self.assertEqual(obj["type"], "tool_result")
        self.assertEqual(obj["id"], "123")
        self.assertTrue(obj["SCRUBBED"])
        self.assertNotIn("content", obj)

    def test_scrub_claude_stream_json_removes_tool_use(self):
        """Test that tool_use events are scrubbed to metadata-only."""
        raw = json.dumps({"type": "tool_use", "id": "456", "name": "read", "input": {"path": "secret.env"}})
        scrubbed = relay._scrub_claude_stream_json(raw)
        obj = json.loads(scrubbed)
        self.assertEqual(obj["type"], "tool_use")
        self.assertEqual(obj["id"], "456")
        self.assertEqual(obj["name"], "read")
        self.assertTrue(obj["SCRUBBED"])
        self.assertNotIn("input", obj)

    def test_scrub_claude_stream_json_preserves_other_events(self):
        """Test that non-tool events are preserved unchanged."""
        raw = json.dumps({"type": "text", "content": "hello world"})
        scrubbed = relay._scrub_claude_stream_json(raw)
        obj = json.loads(scrubbed)
        self.assertEqual(obj["type"], "text")
        self.assertEqual(obj["content"], "hello world")
        self.assertNotIn("SCRUBBED", obj)

    def test_scrub_claude_stream_json_handles_multiline_ndjson(self):
        """Test scrubbing with multiple NDJSON lines."""
        lines = [
            json.dumps({"type": "text", "content": "thinking"}),
            json.dumps({"type": "tool_use", "id": "1", "name": "read", "input": {"path": "/etc/passwd"}}),
            json.dumps({"type": "text", "content": "output"}),
            json.dumps({"type": "tool_result", "id": "1", "content": "root:..."}),
        ]
        raw = "\n".join(lines)
        scrubbed = relay._scrub_claude_stream_json(raw)
        scrubbed_lines = scrubbed.split("\n")

        self.assertEqual(len(scrubbed_lines), 4)
        obj0 = json.loads(scrubbed_lines[0])
        self.assertEqual(obj0["type"], "text")
        self.assertNotIn("SCRUBBED", obj0)

        obj1 = json.loads(scrubbed_lines[1])
        self.assertEqual(obj1["type"], "tool_use")
        self.assertTrue(obj1["SCRUBBED"])

        obj2 = json.loads(scrubbed_lines[2])
        self.assertEqual(obj2["type"], "text")
        self.assertNotIn("SCRUBBED", obj2)

        obj3 = json.loads(scrubbed_lines[3])
        self.assertEqual(obj3["type"], "tool_result")
        self.assertTrue(obj3["SCRUBBED"])

    def test_scrub_claude_stream_json_handles_malformed_json(self):
        """Test that malformed JSON lines are passed through unchanged."""
        lines = [
            json.dumps({"type": "text", "content": "valid"}),
            "not valid json",
            json.dumps({"type": "tool_result", "id": "1", "content": "sensitive"}),
        ]
        raw = "\n".join(lines)
        scrubbed = relay._scrub_claude_stream_json(raw)
        scrubbed_lines = scrubbed.split("\n")

        self.assertEqual(len(scrubbed_lines), 3)
        self.assertEqual(scrubbed_lines[1], "not valid json")
        obj2 = json.loads(scrubbed_lines[2])
        self.assertTrue(obj2["SCRUBBED"])


if __name__ == "__main__":
    unittest.main()
