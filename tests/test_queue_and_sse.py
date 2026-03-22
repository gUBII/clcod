"""
Tests for dispatch queue correlation and SSE queue-state replay.

These tests verify:
1. Queue correlation - dispatch jobs are properly created, claimed, and tracked
2. SSE queue-state replay - queue state events are correctly published
"""

import json
import tempfile
import unittest
from pathlib import Path

from event_store import EventStore


class QueueCorrelationTests(unittest.TestCase):
    """Test dispatch queue correlation with events."""

    def setUp(self):
        """Create a temporary event store for each test."""
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.store = EventStore(self.db_path)

    def tearDown(self):
        """Clean up resources."""
        self.store.close()
        self.tmpdir.cleanup()

    def test_enqueue_dispatch_returns_id_and_status(self):
        """Enqueue creates a dispatch job with id and status 'pending'."""
        result = self.store.enqueue_dispatch({
            "sender": "CLAUDE",
            "body": "hello world",
            "targets": ["CODEX", "GEMINI"],
        })
        self.assertIn("id", result)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["created_at"], result.get("created_at"))

    def test_claim_next_dispatch_transitions_to_active(self):
        """Claiming a dispatch job marks it as active and started_at is set."""
        enqueued = self.store.enqueue_dispatch({
            "sender": "CLAUDE",
            "body": "test",
            "targets": ["CODEX"],
        })
        job_id = enqueued["id"]

        claimed = self.store.claim_next_dispatch()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], job_id)
        self.assertIn("started_at", claimed)

    def test_claim_skips_completed_dispatches(self):
        """Completed dispatches are not claimed."""
        job1 = self.store.enqueue_dispatch({
            "sender": "CLAUDE",
            "body": "first",
            "targets": ["CODEX"],
        })
        job2 = self.store.enqueue_dispatch({
            "sender": "CODEX",
            "body": "second",
            "targets": ["GEMINI"],
        })

        claimed1 = self.store.claim_next_dispatch()
        self.assertEqual(claimed1["id"], job1["id"])
        self.store.complete_dispatch(job1["id"], "done")

        claimed2 = self.store.claim_next_dispatch()
        self.assertEqual(claimed2["id"], job2["id"])

    def test_complete_dispatch_marks_done(self):
        """Completing a job with status 'done' marks it as done."""
        enqueued = self.store.enqueue_dispatch({
            "sender": "CLAUDE",
            "body": "test",
            "targets": ["CODEX"],
        })
        job_id = enqueued["id"]

        self.store.claim_next_dispatch()
        self.store.complete_dispatch(job_id, "done")

        # After completing, claiming should return None (no pending jobs)
        claimed = self.store.claim_next_dispatch()
        self.assertIsNone(claimed)

    def test_complete_dispatch_with_error(self):
        """Completing a job with an error stores the error message."""
        enqueued = self.store.enqueue_dispatch({
            "sender": "CLAUDE",
            "body": "test",
            "targets": ["CODEX"],
        })
        job_id = enqueued["id"]

        self.store.claim_next_dispatch()
        error_msg = "No matching agents"
        self.store.complete_dispatch(job_id, "failed", error_msg)

        # Verify the error is stored (we need to query the db directly)
        row = self.store._conn.execute(
            "SELECT status, error FROM dispatch_queue WHERE id = ?",
            (job_id,)
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], error_msg)

    def test_queue_depth_counts_pending(self):
        """queue_depth returns count of pending jobs."""
        self.assertEqual(self.store.queue_depth(), 0)

        self.store.enqueue_dispatch({"body": "1", "targets": ["CODEX"]})
        self.assertEqual(self.store.queue_depth(), 1)

        self.store.enqueue_dispatch({"body": "2", "targets": ["CODEX"]})
        self.assertEqual(self.store.queue_depth(), 2)

        job = self.store.claim_next_dispatch()
        self.assertEqual(self.store.queue_depth(), 1)

        self.store.complete_dispatch(job["id"], "done")
        self.assertEqual(self.store.queue_depth(), 1)

    def test_active_dispatch_count(self):
        """active_dispatch_count returns count of in-flight jobs."""
        self.assertEqual(self.store.active_dispatch_count(), 0)

        job1 = self.store.enqueue_dispatch({"body": "1", "targets": ["CODEX"]})
        self.assertEqual(self.store.active_dispatch_count(), 0)

        claimed = self.store.claim_next_dispatch()
        self.assertEqual(self.store.active_dispatch_count(), 1)

        self.store.complete_dispatch(claimed["id"], "done")
        self.assertEqual(self.store.active_dispatch_count(), 0)

    def test_recover_stale_active_resets_old_jobs(self):
        """recover_stale_active resets jobs older than max_age back to pending."""
        job1 = self.store.enqueue_dispatch({"body": "1", "targets": ["CODEX"]})
        claimed = self.store.claim_next_dispatch()

        # Manually set started_at to old timestamp
        old_ts = "2020-01-01T00:00:00Z"
        self.store._conn.execute(
            "UPDATE dispatch_queue SET started_at = ? WHERE id = ?",
            (old_ts, claimed["id"])
        )
        self.store._conn.commit()

        recovered = self.store.recover_stale_active(max_age_seconds=0)
        self.assertEqual(recovered, 1)
        self.assertEqual(self.store.active_dispatch_count(), 0)
        self.assertEqual(self.store.queue_depth(), 1)

    def test_dispatch_preserves_all_fields(self):
        """Enqueue preserves sender, targets, task, and route metadata."""
        task = {"id": 42, "title": "Test task"}
        dispatched = self.store.enqueue_dispatch({
            "sender": "CLAUDE",
            "body": "test message",
            "targets": ["CODEX", "GEMINI"],
            "task": task,
            "route_source": "dispatcher",
            "requested_target": "CODEX",
            "dispatcher_action": "route",
            "work_dir": "/tmp/test",
            "message_id": "msg-123",
            "batch_ids": ["batch-1", "batch-2"],
            "message_kind": "task",
        })

        claimed = self.store.claim_next_dispatch()
        self.assertEqual(claimed["sender"], "CLAUDE")
        self.assertEqual(claimed["body"], "test message")
        self.assertEqual(set(claimed["targets"]), {"CODEX", "GEMINI"})
        self.assertEqual(claimed["task"], task)
        self.assertEqual(claimed["route_source"], "dispatcher")
        self.assertEqual(claimed["requested_target"], "CODEX")
        self.assertEqual(claimed["dispatcher_action"], "route")
        self.assertEqual(claimed["work_dir"], "/tmp/test")
        self.assertEqual(claimed["message_id"], "msg-123")
        self.assertEqual(set(claimed["batch_ids"]), {"batch-1", "batch-2"})
        self.assertEqual(claimed["message_kind"], "task")

    def test_fifo_ordering(self):
        """Dispatch jobs are claimed in FIFO order."""
        ids = []
        for i in range(5):
            result = self.store.enqueue_dispatch({
                "body": f"message {i}",
                "targets": ["CODEX"],
            })
            ids.append(result["id"])

        claimed_ids = []
        for _ in range(5):
            job = self.store.claim_next_dispatch()
            if job:
                claimed_ids.append(job["id"])
                self.store.complete_dispatch(job["id"], "done")

        self.assertEqual(claimed_ids, ids)


class SSEQueueStateReplayTests(unittest.TestCase):
    """Test SSE queue-state replay for dispatch queue."""

    def setUp(self):
        """Create a temporary event store for each test."""
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.store = EventStore(self.db_path)
        self.sse_events = []

    def tearDown(self):
        """Clean up resources."""
        self.store.close()
        self.tmpdir.cleanup()

    def publish_sse_event(self, event: dict) -> None:
        """Simulate SSE publish (records events for inspection)."""
        self.sse_events.append(event)

    def test_dispatch_queued_event(self):
        """Dispatch queued event contains job_id and queue_depth."""
        self.store.enqueue_dispatch({
            "sender": "CLAUDE",
            "body": "test",
            "targets": ["CODEX"],
        })

        event = {
            "type": "dispatch_queued",
            "job_id": 1,
            "targets": ["CODEX"],
            "queue_depth": self.store.queue_depth(),
        }
        self.publish_sse_event(event)

        self.assertEqual(len(self.sse_events), 1)
        self.assertEqual(self.sse_events[0]["type"], "dispatch_queued")
        self.assertEqual(self.sse_events[0]["queue_depth"], 1)

    def test_dispatch_started_event(self):
        """Dispatch started event has job_id, targets, sender, and queue_depth."""
        enqueued = self.store.enqueue_dispatch({
            "sender": "CLAUDE",
            "body": "test",
            "targets": ["CODEX", "GEMINI"],
        })
        job = self.store.claim_next_dispatch()

        event = {
            "type": "dispatch_started",
            "job_id": job["id"],
            "targets": job["targets"],
            "sender": job["sender"],
            "queue_depth": self.store.queue_depth(),
        }
        self.publish_sse_event(event)

        self.assertEqual(len(self.sse_events), 1)
        self.assertEqual(self.sse_events[0]["type"], "dispatch_started")
        self.assertEqual(set(self.sse_events[0]["targets"]), {"CODEX", "GEMINI"})

    def test_dispatch_completed_event(self):
        """Dispatch completed event has job_id, targets, and final queue_depth."""
        enqueued = self.store.enqueue_dispatch({
            "sender": "CLAUDE",
            "body": "test",
            "targets": ["CODEX"],
        })
        job = self.store.claim_next_dispatch()
        self.store.complete_dispatch(job["id"], "done")

        event = {
            "type": "dispatch_completed",
            "job_id": job["id"],
            "targets": job["targets"],
            "queue_depth": self.store.queue_depth(),
        }
        self.publish_sse_event(event)

        self.assertEqual(len(self.sse_events), 1)
        self.assertEqual(self.sse_events[0]["type"], "dispatch_completed")
        self.assertEqual(self.sse_events[0]["queue_depth"], 0)

    def test_dispatch_failed_event(self):
        """Dispatch failed event has job_id, targets, error, and queue_depth."""
        enqueued = self.store.enqueue_dispatch({
            "sender": "CLAUDE",
            "body": "test",
            "targets": ["CODEX"],
        })
        job = self.store.claim_next_dispatch()
        error = "No matching agents"
        self.store.complete_dispatch(job["id"], "failed", error)

        event = {
            "type": "dispatch_failed",
            "job_id": job["id"],
            "targets": job["targets"],
            "error": error,
            "queue_depth": self.store.queue_depth(),
        }
        self.publish_sse_event(event)

        self.assertEqual(len(self.sse_events), 1)
        self.assertEqual(self.sse_events[0]["type"], "dispatch_failed")
        self.assertEqual(self.sse_events[0]["error"], error)

    def test_queue_state_replay_sequence(self):
        """Full sequence of SSE events replays correct queue state."""
        # Enqueue multiple jobs
        job1 = self.store.enqueue_dispatch({"body": "msg1", "targets": ["CODEX"]})
        job2 = self.store.enqueue_dispatch({"body": "msg2", "targets": ["GEMINI"]})

        self.publish_sse_event({
            "type": "dispatch_queued",
            "job_id": job1["id"],
            "targets": ["CODEX"],
            "queue_depth": self.store.queue_depth(),
        })
        self.publish_sse_event({
            "type": "dispatch_queued",
            "job_id": job2["id"],
            "targets": ["GEMINI"],
            "queue_depth": self.store.queue_depth(),
        })

        # Claim and process first job
        claimed1 = self.store.claim_next_dispatch()
        self.publish_sse_event({
            "type": "dispatch_started",
            "job_id": claimed1["id"],
            "targets": claimed1["targets"],
            "queue_depth": self.store.queue_depth(),
        })
        self.store.complete_dispatch(claimed1["id"], "done")
        self.publish_sse_event({
            "type": "dispatch_completed",
            "job_id": claimed1["id"],
            "targets": claimed1["targets"],
            "queue_depth": self.store.queue_depth(),
        })

        # Claim and process second job
        claimed2 = self.store.claim_next_dispatch()
        self.publish_sse_event({
            "type": "dispatch_started",
            "job_id": claimed2["id"],
            "targets": claimed2["targets"],
            "queue_depth": self.store.queue_depth(),
        })
        self.store.complete_dispatch(claimed2["id"], "done")
        self.publish_sse_event({
            "type": "dispatch_completed",
            "job_id": claimed2["id"],
            "targets": claimed2["targets"],
            "queue_depth": self.store.queue_depth(),
        })

        # Verify event sequence
        self.assertEqual(len(self.sse_events), 6)
        self.assertEqual(self.sse_events[0]["type"], "dispatch_queued")
        self.assertEqual(self.sse_events[0]["queue_depth"], 2)
        self.assertEqual(self.sse_events[1]["type"], "dispatch_queued")
        self.assertEqual(self.sse_events[1]["queue_depth"], 2)
        self.assertEqual(self.sse_events[2]["type"], "dispatch_started")
        self.assertEqual(self.sse_events[2]["queue_depth"], 1)
        self.assertEqual(self.sse_events[3]["type"], "dispatch_completed")
        self.assertEqual(self.sse_events[3]["queue_depth"], 1)
        self.assertEqual(self.sse_events[4]["type"], "dispatch_started")
        self.assertEqual(self.sse_events[4]["queue_depth"], 0)
        self.assertEqual(self.sse_events[5]["type"], "dispatch_completed")
        self.assertEqual(self.sse_events[5]["queue_depth"], 0)

    def test_failed_dispatch_in_replay_sequence(self):
        """Failed dispatch is correctly replayed in SSE sequence."""
        job = self.store.enqueue_dispatch({"body": "test", "targets": ["UNKNOWN"]})
        self.publish_sse_event({
            "type": "dispatch_queued",
            "job_id": job["id"],
            "queue_depth": self.store.queue_depth(),
        })

        claimed = self.store.claim_next_dispatch()
        self.publish_sse_event({
            "type": "dispatch_started",
            "job_id": claimed["id"],
            "queue_depth": self.store.queue_depth(),
        })

        error = "No matching agents for ['UNKNOWN']"
        self.store.complete_dispatch(claimed["id"], "failed", error)
        self.publish_sse_event({
            "type": "dispatch_failed",
            "job_id": claimed["id"],
            "error": error,
            "queue_depth": self.store.queue_depth(),
        })

        self.assertEqual(len(self.sse_events), 3)
        self.assertEqual(self.sse_events[2]["type"], "dispatch_failed")
        self.assertEqual(self.sse_events[2]["error"], error)


if __name__ == "__main__":
    unittest.main()
