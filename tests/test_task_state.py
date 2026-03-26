import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from event_store import EventStore
import task_state


class TaskProjectionTests(unittest.TestCase):
    def test_rebuild_projection_ignores_unrelated_and_skips_malformed_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "events.db"
            store = EventStore(db_path)
            store.append_event({"type": "relay_state", "state": "running"})
            store.append_event(
                {
                    "type": "task_created",
                    "ts": "2026-03-20T09:00:00Z",
                    "task_id": 3,
                    "status": "pending",
                    "task": {
                        "id": 3,
                        "title": "Initial",
                        "type": "general",
                        "status": "pending",
                        "priority": "normal",
                        "assigned_to": [],
                        "source_message": "/task Initial",
                        "created_at": "2026-03-20T09:00:00Z",
                        "completed_at": None,
                        "tokens_spent": 0,
                    },
                }
            )
            store.append_event({"type": "task_updated", "task": {"id": "nope"}})
            store.append_event(
                {
                    "type": "tasks_updated",
                    "new_status": "in_progress",
                    "tasks": [
                        {
                            "id": 3,
                            "title": "Initial",
                            "type": "general",
                            "status": "in_progress",
                            "priority": "normal",
                            "assigned_to": ["CODEX"],
                            "source_message": "/task Initial",
                            "created_at": "2026-03-20T09:00:00Z",
                            "completed_at": None,
                            "tokens_spent": 0,
                        }
                    ],
                }
            )

            projection = task_state.rebuild_tasks_projection_from_events(store)

            self.assertEqual(len(projection["tasks"]), 1)
            self.assertEqual(projection["tasks"][0]["status"], "in_progress")
            self.assertEqual(projection["tasks"][0]["assigned_to"], ["CODEX"])
            self.assertEqual(projection["next_id"], 4)

            summary = task_state.rebuild_task_summary_from_projection(projection)
            self.assertEqual(summary["pending"], 0)
            self.assertEqual(summary["in_progress"], 1)
            self.assertEqual(summary["done"], 0)

            store.close()

    def test_summary_counts_assigned_as_in_progress(self):
        summary = task_state.rebuild_task_summary_from_projection(
            {
                "next_id": 4,
                "tasks": [
                    {"id": 1, "status": "pending", "created_at": "2026-03-20T09:00:00Z"},
                    {"id": 2, "status": "assigned", "created_at": "2026-03-20T09:01:00Z"},
                    {"id": 3, "status": "done", "created_at": "2026-03-20T09:02:00Z"},
                ],
            }
        )

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["in_progress"], 1)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["last_created_at"], "2026-03-20T09:02:00Z")


class TaskStateManagerTests(unittest.TestCase):
    def test_rebuild_restores_tasks_file_after_corruption(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tasks_path = Path(tmpdir) / "tasks.json"
            store = EventStore(Path(tmpdir) / "events.db")
            manager = task_state.TaskStateManager(event_store=store, tasks_path=tasks_path)
            manager.rebuild_from_events()
            task = manager.create_task_command(title="Recover me", assigned_to=["CLAUDE"])

            tasks_path.write_text("{broken", encoding="utf-8")

            repair = task_state.TaskStateManager(event_store=store, tasks_path=tasks_path)
            repair.rebuild_from_events()

            restored = json.loads(tasks_path.read_text(encoding="utf-8"))
            self.assertEqual(restored["tasks"][0]["id"], task["id"])
            self.assertEqual(restored["next_id"], task["id"] + 1)
            store.close()

    def test_seed_legacy_tasks_only_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tasks_path = Path(tmpdir) / "tasks.json"
            tasks_path.write_text(
                json.dumps(
                    {
                        "next_id": 11,
                        "tasks": [
                            {
                                "id": 10,
                                "title": "Legacy",
                                "type": "general",
                                "status": "done",
                                "priority": "normal",
                                "assigned_to": ["CODEX"],
                                "source_message": "/task Legacy",
                                "created_at": "2026-03-20T09:00:00Z",
                                "completed_at": "2026-03-20T09:05:00Z",
                                "tokens_spent": 0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = EventStore(Path(tmpdir) / "events.db")

            manager = task_state.TaskStateManager(event_store=store, tasks_path=tasks_path)
            first = manager.rebuild_from_events()
            second = manager.rebuild_from_events()

            self.assertEqual(first["seeded"], 1)
            self.assertEqual(second["seeded"], 0)
            self.assertEqual(store.count_events(task_state.TASK_REPLAY_EVENT_TYPES), 1)
            self.assertEqual(manager.snapshot()["next_id"], 11)
            store.close()

    def test_projection_write_failure_keeps_committed_event_and_replay_repairs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tasks_path = Path(tmpdir) / "tasks.json"
            store = EventStore(Path(tmpdir) / "events.db")
            manager = task_state.TaskStateManager(event_store=store, tasks_path=tasks_path)
            manager.rebuild_from_events()
            task = manager.create_task_command(title="Needs repair")

            with mock.patch("task_state.save_tasks_projection", side_effect=OSError("disk full")):
                updated = manager.update_task_command(task["id"], status="done")

            self.assertEqual(updated["status"], "done")
            self.assertEqual(store.count_events(task_state.TASK_REPLAY_EVENT_TYPES), 2)

            repair = task_state.TaskStateManager(event_store=store, tasks_path=tasks_path)
            repair.rebuild_from_events()
            restored = json.loads(tasks_path.read_text(encoding="utf-8"))

            self.assertEqual(restored["tasks"][0]["status"], "done")
            store.close()

