import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import relay
import supervisor


class SupervisorTests(unittest.TestCase):
    def test_parse_transcript_entries_groups_tagged_blocks(self):
        text = "[FARHAN]\nhello\n\n[CODEX]\nhi there\n"

        entries = supervisor.parse_transcript_entries(text, limit=10)

        self.assertEqual(
            entries,
            [
                {"speaker": "FARHAN", "text": "hello"},
                {"speaker": "CODEX", "text": "hi there"},
            ],
        )

    def test_desired_mirror_view_falls_back_to_log_without_session(self):
        agent = {
            "mirror_mode": "resume",
            "mirror_resume_args": ["resume", "{session_id}"],
        }

        self.assertEqual(supervisor.desired_mirror_view(agent, None), "log")
        self.assertEqual(supervisor.desired_mirror_view(agent, "abc"), "resume")

    def test_infer_agent_state_preserves_error(self):
        self.assertEqual(
            supervisor.infer_agent_state("error", "running", "resume", "codex"),
            "error",
        )

    def test_infer_agent_state_moves_ready_when_relay_running_and_pane_alive(self):
        self.assertEqual(
            supervisor.infer_agent_state("auth", "running", "log", "tail"),
            "ready",
        )

    def test_build_agent_state_exposes_control_metadata(self):
        payload = supervisor.build_agent_state(
            {
                "mirror_mode": "resume",
                "selected_model": "sonnet",
                "selected_effort": "high",
                "model_options": [{"id": "default"}, {"id": "sonnet"}],
                "effort_options": [{"id": "default"}, {"id": "high"}],
                "effort_matrix": {"sonnet": ["default", "high"]},
            }
        )

        self.assertEqual(payload["selected_model"], "sonnet")
        self.assertEqual(payload["selected_effort"], "high")
        self.assertEqual(payload["mirror_mode"], "resume")
        self.assertEqual(payload["effort_matrix"], {"sonnet": ["default", "high"]})

    def test_build_resume_mirror_command_prefixes_selection_args(self):
        command = supervisor.build_resume_mirror_command(
            {
                "cmd": "claude",
                "mirror_resume_args": ["--resume", "{session_id}"],
                "model_arg": ["--model", "{value}"],
                "effort_arg": ["--effort", "{value}"],
                "model_options": [{"id": "default", "value": None}, {"id": "sonnet", "value": "sonnet"}],
                "effort_options": [{"id": "default", "value": None}, {"id": "high", "value": "high"}],
                "selected_model": "sonnet",
                "selected_effort": "high",
            },
            "session-1",
        )

        self.assertIn("claude --model sonnet --effort high --resume session-1", command)

    def test_build_log_mirror_command_uses_work_dir(self):
        command = supervisor.build_log_mirror_command(
            {
                "name": "CLAUDE",
                "io_log_path": Path("/tmp/claude.log"),
                "work_dir": "/tmp/demo-repo",
            }
        )

        self.assertTrue(command.startswith("cd /tmp/demo-repo &&"))

    def test_refresh_task_state_counts_statuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workspace": {
                            "log_path": "room.txt",
                            "relay_log_path": ".clcod-runtime/relay.log",
                            "state_path": ".clcod-runtime/state.json",
                            "sessions_path": ".clcod-runtime/sessions.json",
                            "preferences_path": ".clcod-runtime/preferences.json",
                            "projects_path": ".clcod-runtime/projects.json",
                            "tasks_path": ".clcod-runtime/tasks.json",
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = relay.load_config(config_path)
            runtime = supervisor.RuntimeSupervisor(config)
            relay.save_tasks(
                config["workspace"]["tasks_path"],
                {
                    "next_id": 4,
                    "tasks": [
                        {"id": 1, "status": "pending", "created_at": "2026-03-20T09:00:00Z"},
                        {"id": 2, "status": "assigned", "created_at": "2026-03-20T09:01:00Z"},
                        {"id": 3, "status": "done", "created_at": "2026-03-20T09:02:00Z"},
                    ],
                },
            )

            runtime.refresh_task_state()
            snapshot = runtime.state.snapshot()["tasks"]

            self.assertEqual(snapshot["total"], 3)
            self.assertEqual(snapshot["pending"], 1)
            self.assertEqual(snapshot["in_progress"], 1)
            self.assertEqual(snapshot["done"], 1)
            self.assertEqual(snapshot["last_created_at"], "2026-03-20T09:02:00Z")

    def test_lock_project_sets_active_project_and_agent_workdirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            repo_dir.mkdir()
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workspace": {
                            "log_path": "room.txt",
                            "relay_log_path": ".clcod-runtime/relay.log",
                            "state_path": ".clcod-runtime/state.json",
                            "sessions_path": ".clcod-runtime/sessions.json",
                            "preferences_path": ".clcod-runtime/preferences.json",
                            "projects_path": ".clcod-runtime/projects.json",
                            "tasks_path": ".clcod-runtime/tasks.json",
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = relay.load_config(config_path)
            runtime = supervisor.RuntimeSupervisor(config)
            runtime.sync_agent_mirrors = mock.Mock()

            projects = runtime.lock_project(path=str(repo_dir))

            self.assertEqual(projects["active"], "repo")
            self.assertEqual(projects["projects"]["repo"]["path"], str(repo_dir.resolve()))
            self.assertTrue(all(agent.get("work_dir") == str(repo_dir.resolve()) for agent in config["agents"] if agent["enabled"]))
            runtime.sync_agent_mirrors.assert_called_once_with(force=True)

    def test_handle_relay_event_updates_transcript_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workspace": {
                            "log_path": "room.txt",
                            "relay_log_path": ".clcod-runtime/relay.log",
                            "state_path": ".clcod-runtime/state.json",
                            "sessions_path": ".clcod-runtime/sessions.json",
                            "preferences_path": ".clcod-runtime/preferences.json",
                            "projects_path": ".clcod-runtime/projects.json",
                            "tasks_path": ".clcod-runtime/tasks.json",
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = relay.load_config(config_path)
            runtime = supervisor.RuntimeSupervisor(config)
            runtime.sse_broadcast = mock.Mock()

            runtime.handle_relay_event(
                {
                    "type": "transcript",
                    "last_speaker": "CODEX",
                    "last_updated_at": "2026-03-20T10:00:00Z",
                    "char_count": 40,
                }
            )

            snapshot = runtime.state.snapshot()["transcript"]
            self.assertEqual(snapshot["last_speaker"], "CODEX")
            self.assertEqual(snapshot["last_updated_at"], "2026-03-20T10:00:00Z")
            runtime.sse_broadcast.assert_called_once_with("transcript", {"last_speaker": "CODEX"})

    def test_handle_relay_event_updates_agent_pressure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workspace": {
                            "log_path": "room.txt",
                            "relay_log_path": ".clcod-runtime/relay.log",
                            "state_path": ".clcod-runtime/state.json",
                            "sessions_path": ".clcod-runtime/sessions.json",
                            "preferences_path": ".clcod-runtime/preferences.json",
                            "projects_path": ".clcod-runtime/projects.json",
                            "tasks_path": ".clcod-runtime/tasks.json",
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = relay.load_config(config_path)
            runtime = supervisor.RuntimeSupervisor(config)
            runtime.sse_broadcast = mock.Mock()

            runtime.handle_relay_event({"type": "agent_state", "agent": "CLAUDE", "state": "working"})
            runtime.handle_relay_event(
                {
                    "type": "agent_state",
                    "agent": "CLAUDE",
                    "state": "ready",
                    "tokens_delta": 24,
                }
            )

            agent = runtime.state.snapshot()["agents"]["CLAUDE"]
            self.assertEqual(agent["state"], "ready")
            self.assertIn("pressure", agent)
            self.assertGreaterEqual(agent["pressure"]["last_latency_ms"], 0)
            runtime.sse_broadcast.assert_any_call("agent_state", {"agent": "CLAUDE", "state": "working"})
            runtime.sse_broadcast.assert_any_call("agent_state", {"agent": "CLAUDE", "state": "ready"})


if __name__ == "__main__":
    unittest.main()
