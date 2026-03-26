#!/usr/bin/env python3
"""
SQLite-backed event spine for clcod.

Schema versions:
  1 - Phase 1: events table with nullable target
  2 - Phase 2: dispatch_queue table for queue-backed dispatch
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
LOGGER = logging.getLogger(__name__)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def stable_transcript_id(sender: str, body: str, ts: str, seq: int) -> str:
    raw = "\n".join([sender, body, ts, str(seq)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def parse_transcript_lines(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    tagged_speaker: str | None = None
    tagged_lines: list[str] = []

    def flush_tagged() -> None:
        nonlocal tagged_speaker, tagged_lines
        if tagged_speaker and tagged_lines:
            body = "\n".join(tagged_lines).strip()
            if body:
                ts = utc_now()
                seq = int(time.time() * 1000)
                entries.append(
                    {
                        "id": stable_transcript_id(tagged_speaker, body, ts, seq),
                        "sender": tagged_speaker,
                        "body": body,
                        "seq": seq,
                        "ts": ts,
                        "type": "message",
                    }
                )
        tagged_speaker = None
        tagged_lines = []

    for line in text.splitlines():
        raw_line = line.rstrip()
        stripped = raw_line.strip()
        if not stripped:
            flush_tagged()
            continue
        if stripped.startswith("[") and stripped.endswith("]") and len(stripped) > 2:
            flush_tagged()
            tagged_speaker = stripped[1:-1].strip()
            continue
        if tagged_speaker:
            tagged_lines.append(raw_line)
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "sender" in payload and "body" in payload:
            item = dict(payload)
            item.setdefault("seq", int(time.time() * 1000))
            item.setdefault("ts", utc_now())
            item.setdefault(
                "id",
                stable_transcript_id(
                    str(item.get("sender") or ""),
                    str(item.get("body") or ""),
                    str(item.get("ts") or ""),
                    int(item.get("seq") or 0),
                ),
            )
            item.setdefault("type", "message")
            entries.append(item)
        elif isinstance(payload, dict) and "speaker" in payload and "text" in payload:
            ts = str(payload.get("ts") or utc_now())
            seq = int(payload.get("seq") or time.time() * 1000)
            sender = str(payload["speaker"])
            body = str(payload["text"])
            entries.append(
                {
                    "id": stable_transcript_id(sender, body, ts, seq),
                    "sender": sender,
                    "body": body,
                    "seq": seq,
                    "ts": ts,
                    "type": "message",
                }
            )
    flush_tagged()
    return entries


class EventStore:
    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Schema migrations ────────────────────────────────────────

    def _init_schema(self) -> None:
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]

        if current < 1:
            existing = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
            ).fetchone()
            if existing:
                pragma_rows = self._conn.execute("PRAGMA table_info(events)").fetchall()
                needs_migration = any(
                    row[1] == "target" and row[3] for row in pragma_rows
                )
                if needs_migration:
                    self._conn.executescript(
                        """
                        ALTER TABLE events RENAME TO _events_migrate;
                        CREATE TABLE events (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            ts TEXT NOT NULL,
                            type TEXT NOT NULL,
                            correlation_id TEXT,
                            task_id INTEGER,
                            sender TEXT,
                            target TEXT,
                            status TEXT,
                            payload TEXT NOT NULL
                        );
                        INSERT INTO events SELECT * FROM _events_migrate;
                        DROP TABLE _events_migrate;
                        """
                    )
            else:
                self._conn.executescript(
                    """
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        type TEXT NOT NULL,
                        correlation_id TEXT,
                        task_id INTEGER,
                        sender TEXT,
                        target TEXT,
                        status TEXT,
                        payload TEXT NOT NULL
                    );
                    """
                )
            self._conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS events_type_id_idx ON events(type, id);
                CREATE INDEX IF NOT EXISTS events_corr_idx ON events(correlation_id);
                """
            )
            self._conn.execute("PRAGMA user_version = 1")
            self._conn.commit()
            current = 1

        if current < 2:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS dispatch_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    sender TEXT,
                    body TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    task_json TEXT,
                    route_source TEXT,
                    requested_target TEXT,
                    dispatcher_action TEXT,
                    work_dir TEXT,
                    message_id TEXT,
                    batch_ids_json TEXT,
                    message_kind TEXT DEFAULT 'message',
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS dq_status_idx
                    ON dispatch_queue(status, id);
                """
            )
            self._conn.execute("PRAGMA user_version = 2")
            self._conn.commit()

    # ── Event store operations ───────────────────────────────────

    def _prepare_event_insert(self, event_data: dict[str, Any]) -> tuple[str, str, Any, Any, Any, Any, Any, dict[str, Any]]:
        payload = dict(event_data)
        event_type = str(payload.pop("type"))
        payload.pop("event_id", None)
        ts = str(payload.get("ts") or utc_now())
        correlation_id = payload.get("correlation_id")
        task_id = payload.get("task_id")
        sender = payload.get("sender") or payload.get("last_speaker")
        target = payload.get("target")
        if not target and isinstance(payload.get("route"), dict):
            target = payload["route"].get("target")
        if not target and payload.get("agent"):
            target = payload.get("agent")
        status = payload.get("status") or payload.get("state")
        return (
            event_type,
            ts,
            correlation_id,
            task_id,
            sender,
            target,
            status,
            payload,
        )

    def _decode_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except json.JSONDecodeError as exc:
            LOGGER.warning(
                "event_store: skipping malformed payload JSON for event id=%s type=%s: %s",
                row["id"],
                row["type"],
                exc,
            )
            payload = {}
        event = {"id": row["id"], "ts": row["ts"], "type": row["type"], **payload}
        if row["correlation_id"] and "correlation_id" not in event:
            event["correlation_id"] = row["correlation_id"]
        if row["task_id"] is not None and "task_id" not in event:
            event["task_id"] = row["task_id"]
        if row["sender"] and "sender" not in event:
            event["sender"] = row["sender"]
        if row["target"] and "target" not in event:
            event["target"] = row["target"]
        if row["status"] and "status" not in event:
            event["status"] = row["status"]
        return event

    def append_event(self, event_data: dict[str, Any]) -> dict[str, Any]:
        event_type, ts, correlation_id, task_id, sender, target, status, payload = (
            self._prepare_event_insert(event_data)
        )

        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO events (ts, type, correlation_id, task_id, sender, target, status, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    event_type,
                    correlation_id,
                    task_id,
                    sender,
                    target,
                    status,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            self._conn.commit()
            event_id = cur.lastrowid
        return {"id": event_id, "ts": ts, "type": event_type, **payload}

    def append_events(self, event_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not event_data:
            return []

        prepared = [self._prepare_event_insert(item) for item in event_data]
        stored: list[dict[str, Any]] = []
        with self._lock:
            cur = self._conn.cursor()
            for event_type, ts, correlation_id, task_id, sender, target, status, payload in prepared:
                cur.execute(
                    """
                    INSERT INTO events (ts, type, correlation_id, task_id, sender, target, status, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        event_type,
                        correlation_id,
                        task_id,
                        sender,
                        target,
                        status,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                stored.append({"id": cur.lastrowid, "ts": ts, "type": event_type, **payload})
            self._conn.commit()
        return stored

    def list_events(self, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, ts, type, correlation_id, task_id, sender, target, status, payload
                FROM events
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (max(0, int(after_id)), max(1, min(limit, 1000))),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def latest_event_id(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM events").fetchone()
        return int(row["max_id"] if row else 0)

    def count_events(self, types: list[str] | tuple[str, ...] | set[str] | None = None) -> int:
        with self._lock:
            if types:
                values = tuple(str(item) for item in types)
                placeholders = ", ".join("?" for _ in values)
                row = self._conn.execute(
                    f"SELECT COUNT(*) AS cnt FROM events WHERE type IN ({placeholders})",
                    values,
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) AS cnt FROM events").fetchone()
        return int(row["cnt"]) if row else 0

    def has_events(self, types: list[str] | tuple[str, ...] | set[str] | None = None) -> bool:
        return self.count_events(types) > 0

    def import_transcript(self, transcript_path: Path | str) -> int:
        path = Path(transcript_path)
        if not path.exists():
            return 0
        entries = parse_transcript_lines(path.read_text(encoding="utf-8", errors="replace"))
        imported = 0
        for entry in entries:
            correlation_id = str(entry.get("id") or "")
            with self._lock:
                existing = self._conn.execute(
                    """
                    SELECT 1
                    FROM events
                    WHERE type = 'transcript' AND correlation_id = ?
                    LIMIT 1
                    """,
                    (correlation_id,),
                ).fetchone()
            if existing:
                continue
            self.append_event(
                {
                    "type": "transcript",
                    "correlation_id": correlation_id,
                    "ts": entry.get("ts") or utc_now(),
                    "sender": entry.get("sender"),
                    "last_speaker": entry.get("sender"),
                    "last_updated_at": entry.get("ts") or utc_now(),
                    "char_count": len(str(entry.get("body") or "")),
                    "message": entry,
                    "message_type": entry.get("type", "message"),
                    "imported": True,
                }
            )
            imported += 1
        return imported

    # ── Dispatch queue operations ────────────────────────────────

    def enqueue_dispatch(self, data: dict[str, Any]) -> dict[str, Any]:
        """Enqueue a dispatch job. Returns the created queue item with id and status."""
        ts = utc_now()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO dispatch_queue
                    (created_at, status, sender, body, targets_json, task_json,
                     route_source, requested_target, dispatcher_action,
                     work_dir, message_id, batch_ids_json, message_kind)
                VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    data.get("sender"),
                    data["body"],
                    json.dumps(data.get("targets", [])),
                    json.dumps(data.get("task")) if data.get("task") else None,
                    data.get("route_source"),
                    data.get("requested_target"),
                    data.get("dispatcher_action"),
                    data.get("work_dir"),
                    data.get("message_id"),
                    json.dumps(data.get("batch_ids", [])),
                    data.get("message_kind", "message"),
                ),
            )
            self._conn.commit()
        return {"id": cur.lastrowid, "created_at": ts, "status": "pending"}

    def claim_next_dispatch(self) -> dict[str, Any] | None:
        """Atomically claim the oldest pending dispatch job. Returns None if queue is empty."""
        ts = utc_now()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, created_at, sender, body, targets_json, task_json,
                       route_source, requested_target, dispatcher_action,
                       work_dir, message_id, batch_ids_json, message_kind
                FROM dispatch_queue
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            job_id = row["id"]
            self._conn.execute(
                "UPDATE dispatch_queue SET status = 'active', started_at = ? WHERE id = ?",
                (ts, job_id),
            )
            self._conn.commit()
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "sender": row["sender"],
            "body": row["body"],
            "targets": json.loads(row["targets_json"]),
            "task": json.loads(row["task_json"]) if row["task_json"] else None,
            "route_source": row["route_source"],
            "requested_target": row["requested_target"],
            "dispatcher_action": row["dispatcher_action"],
            "work_dir": row["work_dir"],
            "message_id": row["message_id"],
            "batch_ids": json.loads(row["batch_ids_json"]) if row["batch_ids_json"] else [],
            "message_kind": row["message_kind"],
            "status": "active",
            "started_at": ts,
        }

    def complete_dispatch(
        self, job_id: int, status: str = "done", error: str | None = None
    ) -> None:
        """Mark a dispatch job as done or failed."""
        ts = utc_now()
        with self._lock:
            self._conn.execute(
                "UPDATE dispatch_queue SET status = ?, completed_at = ?, error = ? WHERE id = ?",
                (status, ts, error, job_id),
            )
            self._conn.commit()

    def queue_depth(self) -> int:
        """Count of pending dispatch jobs."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM dispatch_queue WHERE status = 'pending'"
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def active_dispatch_count(self) -> int:
        """Count of currently active (in-flight) dispatch jobs."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM dispatch_queue WHERE status = 'active'"
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def recover_stale_active(self, max_age_seconds: int = 600) -> int:
        """Reset active dispatch jobs older than max_age_seconds back to pending."""
        cutoff = time.time() - max_age_seconds
        cutoff_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE dispatch_queue
                SET status = 'pending', started_at = NULL
                WHERE status = 'active' AND started_at < ?
                """,
                (cutoff_ts,),
            )
            self._conn.commit()
        return cur.rowcount


def import_transcript_to_event_store(event_store: EventStore, transcript_path: Path | str) -> int:
    return event_store.import_transcript(transcript_path)
