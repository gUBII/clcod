#!/usr/bin/env python3
"""
SQLite-backed event spine for clcod.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


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

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
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

            CREATE INDEX IF NOT EXISTS events_type_id_idx
              ON events(type, id);

            CREATE INDEX IF NOT EXISTS events_corr_idx
              ON events(correlation_id);
            """
        )
        self._conn.commit()

    def _decode_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload"]) if row["payload"] else {}
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
        return {"id": event_id, "type": event_type, **payload}

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


def import_transcript_to_event_store(event_store: EventStore, transcript_path: Path | str) -> int:
    return event_store.import_transcript(transcript_path)
