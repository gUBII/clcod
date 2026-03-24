import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def test_collect_pane_commands_maps_window_targets_and_pane_ids(self):
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
            runtime.tmux_session_exists = mock.Mock(return_value=True)
            runtime.tmux = mock.Mock(
                return_value=SimpleNamespace(
                    stdout="%7\ttriagent:CODEX.0\ttriagent:2.0\tcodex\n"
                )
            )

            pane_commands = runtime.collect_pane_commands()

            self.assertEqual(pane_commands["%7"], "codex")
            self.assertEqual(pane_commands["triagent:CODEX.0"], "codex")
            self.assertEqual(pane_commands["triagent:2.0"], "codex")

    def test_refresh_tmux_state_marks_named_window_targets_ready(self):
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
            runtime.tmux_session_exists = mock.Mock(return_value=True)
            runtime.sync_agent_mirrors = mock.Mock()
            runtime.collect_pane_commands = mock.Mock(
                return_value={"triagent:CODEX.0": "codex"}
            )
            runtime.state.patch("relay", {"state": "running"})
            runtime.state.patch_agent(
                "CODEX",
                {
                    "pane_target": "triagent:CODEX.0",
                    "mirror_view": "resume",
                    "state": "starting",
                },
            )

            runtime.refresh_tmux_state()

            agent = runtime.state.snapshot()["agents"]["CODEX"]
            self.assertEqual(agent["pane_command"], "codex")
            self.assertEqual(agent["state"], "ready")

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
                    "message": {
                        "id": "msg-1",
                        "sender": "CODEX",
                        "seq": 123,
                        "type": "message",
                        "body": "hello",
                        "ts": "2026-03-20T10:00:00Z",
                    },
                }
            )

            snapshot = runtime.state.snapshot()["transcript"]
            self.assertEqual(snapshot["last_speaker"], "CODEX")
            self.assertEqual(snapshot["last_updated_at"], "2026-03-20T10:00:00Z")
            self.assertEqual(snapshot["rev"], 1)
            runtime.sse_broadcast.assert_called_once_with(
                "transcript",
                {
                    "last_speaker": "CODEX",
                    "rev": 1,
                    "message": {
                        "id": "msg-1",
                        "sender": "CODEX",
                        "seq": 123,
                        "type": "message",
                        "body": "hello",
                        "ts": "2026-03-20T10:00:00Z",
                    },
                },
                None,
            )

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

            runtime.handle_relay_event({"type": "agent_state", "agent": "CLAUDE", "state": "warming"})
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
            runtime.sse_broadcast.assert_any_call(
                "agent_state", {"agent": "CLAUDE", "state": "warming", "last_error": None}, None
            )
            runtime.sse_broadcast.assert_any_call(
                "agent_state", {"agent": "CLAUDE", "state": "ready", "last_error": None}, None
            )

    def test_handle_relay_event_warming_sets_dispatch_ts(self):
        """BUG-3: Pressure tracking must trigger on 'warming', not 'working'."""
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

            # "warming" should set dispatch_ts > 0
            runtime.handle_relay_event({"type": "agent_state", "agent": "CLAUDE", "state": "warming"})
            agent = runtime.state.snapshot()["agents"]["CLAUDE"]
            self.assertGreater(agent["pressure"]["dispatch_ts"], 0)
            self.assertEqual(agent["pressure"]["queue_depth"], 1)

    def test_handle_relay_event_dispatcher_enriched_broadcast(self):
        """BUG-11: Dispatcher SSE must include counter updates."""
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

            runtime.handle_relay_event({
                "type": "dispatcher",
                "action": "absorb",
                "targets": [],
            })

            broadcast_call = runtime.sse_broadcast.call_args
            self.assertEqual(broadcast_call[0][0], "dispatcher")
            payload = broadcast_call[0][1]
            self.assertIn("routes_total", payload)
            self.assertIn("absorbs_total", payload)
            self.assertIn("tokens_saved", payload)
            self.assertEqual(payload["absorbs_total"], 1)

    def test_handle_relay_event_tracks_active_and_completed_routes(self):
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
                    "type": "route_state",
                    "route_id": "route-42",
                    "task_id": 42,
                    "task_title": "Verify route bus",
                    "sender": "FARHAN",
                    "target": "CODEX",
                    "source": "dispatcher",
                    "message_kind": "task",
                    "started_at": "2026-03-22T05:00:00Z",
                    "updated_at": "2026-03-22T05:00:00Z",
                    "status": "transmitting",
                    "tx_state": "active",
                    "rx_state": "waiting",
                }
            )

            routing = runtime.state.snapshot()["routing"]
            self.assertEqual(len(routing["active"]), 1)
            self.assertEqual(routing["active"][0]["route_id"], "route-42")
            self.assertEqual(routing["recent"], [])

            runtime.handle_relay_event(
                {
                    "type": "route_state",
                    "route_id": "route-42",
                    "task_id": 42,
                    "task_title": "Verify route bus",
                    "sender": "FARHAN",
                    "target": "CODEX",
                    "source": "dispatcher",
                    "message_kind": "task",
                    "started_at": "2026-03-22T05:00:00Z",
                    "updated_at": "2026-03-22T05:00:03Z",
                    "completed_at": "2026-03-22T05:00:03Z",
                    "status": "complete",
                    "tx_state": "sent",
                    "rx_state": "received",
                }
            )

            routing = runtime.state.snapshot()["routing"]
            self.assertEqual(routing["active"], [])
            self.assertEqual(len(routing["recent"]), 1)
            self.assertEqual(routing["recent"][0]["route_id"], "route-42")
            runtime.sse_broadcast.assert_any_call(
                "route_state",
                {
                    "route": {
                        "route_id": "route-42",
                        "task_id": 42,
                        "task_title": "Verify route bus",
                        "sender": "FARHAN",
                        "target": "CODEX",
                        "source": "dispatcher",
                        "message_kind": "task",
                        "started_at": "2026-03-22T05:00:00Z",
                        "updated_at": "2026-03-22T05:00:03Z",
                        "completed_at": "2026-03-22T05:00:03Z",
                        "status": "complete",
                        "tx_state": "sent",
                        "rx_state": "received",
                    },
                    "active": [],
                    "recent": [
                        {
                            "route_id": "route-42",
                            "task_id": 42,
                            "task_title": "Verify route bus",
                            "sender": "FARHAN",
                            "target": "CODEX",
                            "source": "dispatcher",
                            "message_kind": "task",
                            "started_at": "2026-03-22T05:00:00Z",
                            "updated_at": "2026-03-22T05:00:03Z",
                            "completed_at": "2026-03-22T05:00:03Z",
                            "status": "complete",
                            "tx_state": "sent",
                            "rx_state": "received",
                        }
                    ],
                    "last_route_at": "2026-03-22T05:00:03Z",
                },
                None,
            )


class SSESubscriberTests(unittest.TestCase):
    """Tests for SSE subscriber cap and queue cleanup."""

    def _make_runtime(self, max_subscribers=3):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "ui": {
                            "max_sse_subscribers": max_subscribers,
                        },
                        "workspace": {
                            "log_path": "room.txt",
                            "relay_log_path": ".clcod-runtime/relay.log",
                            "state_path": ".clcod-runtime/state.json",
                            "sessions_path": ".clcod-runtime/sessions.json",
                            "preferences_path": ".clcod-runtime/preferences.json",
                            "projects_path": ".clcod-runtime/projects.json",
                            "tasks_path": ".clcod-runtime/tasks.json",
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = relay.load_config(config_path)
            return supervisor.RuntimeSupervisor(config)

    def test_subscribe_respects_max_limit(self):
        runtime = self._make_runtime(max_subscribers=2)

        q1 = runtime.sse_subscribe()
        q2 = runtime.sse_subscribe()
        q3 = runtime.sse_subscribe()

        self.assertIsNotNone(q1)
        self.assertIsNotNone(q2)
        self.assertIsNone(q3)
        self.assertEqual(runtime.sse_client_count(), 2)

    def test_unsubscribe_frees_slot(self):
        runtime = self._make_runtime(max_subscribers=1)

        q1 = runtime.sse_subscribe()
        self.assertIsNotNone(q1)
        self.assertIsNone(runtime.sse_subscribe())

        runtime.sse_unsubscribe(q1)
        q2 = runtime.sse_subscribe()
        self.assertIsNotNone(q2)

    def test_broadcast_drops_full_queues(self):
        runtime = self._make_runtime(max_subscribers=5)

        q1 = runtime.sse_subscribe()
        q2 = runtime.sse_subscribe()

        # Fill q1 to capacity (maxsize=64)
        for i in range(64):
            q1.put_nowait({"event_id": None, "payload": {"type": "filler", "i": i}})

        # Broadcast should drop q1 (full) and keep q2
        runtime.sse_broadcast("test_event", {"data": "hello"})

        self.assertEqual(runtime.sse_client_count(), 1)
        frame = q2.get_nowait()
        self.assertEqual(frame["payload"]["type"], "test_event")


if __name__ == "__main__":
    unittest.main()
