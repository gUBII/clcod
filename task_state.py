#!/usr/bin/env python3
"""
Durable task state helpers.

Task lifecycle changes are replayable and event-first:
events.db is the durable source of truth, while tasks.json and the task
section of state.json are derived projections that can be rebuilt at startup.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from event_store import EventStore


LOGGER = logging.getLogger(__name__)

EventCallback = Callable[[dict[str, Any]], None]
DEFAULT_TASKS: dict[str, Any] = {"tasks": [], "next_id": 1}
TASK_STATUSES = {"pending", "assigned", "in_progress", "review", "done", "blocked", "failed"}
TASK_EVENT_TYPES = {"task_created", "task_updated", "tasks_bulk_updated", "tasks_cleared"}
TASK_REPLAY_EVENT_TYPES = TASK_EVENT_TYPES | {"tasks_updated"}
UNCHANGED = object()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    os.replace(temp_name, path)


def _deepcopy_default_tasks() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_TASKS)


def _normalize_assigned_to(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _normalize_tokens_spent(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_task_snapshot(task: Any, *, event_id: int | None = None, context: str = "task") -> dict[str, Any] | None:
    if not isinstance(task, dict):
        LOGGER.warning("task_state: skipping malformed %s for event id=%s", context, event_id)
        return None
    raw_id = task.get("id")
    if isinstance(raw_id, bool):
        raw_id = None
    try:
        task_id = int(raw_id)
    except (TypeError, ValueError):
        LOGGER.warning(
            "task_state: skipping %s with invalid id=%r for event id=%s",
            context,
            raw_id,
            event_id,
        )
        return None

    normalized = dict(task)
    normalized["id"] = task_id
    normalized["title"] = str(normalized.get("title") or "")
    normalized["type"] = str(normalized.get("type") or "general")
    normalized["status"] = str(normalized.get("status") or "pending")
    normalized["priority"] = str(normalized.get("priority") or "normal")
    normalized["assigned_to"] = _normalize_assigned_to(normalized.get("assigned_to"))
    normalized["source_message"] = str(normalized.get("source_message") or "")
    normalized.setdefault("created_at", None)
    normalized.setdefault("completed_at", None)
    normalized["tokens_spent"] = _normalize_tokens_spent(normalized.get("tokens_spent"))
    return normalized


def _normalized_tasks_from_iterable(tasks: Any, *, event_id: int | None = None, context: str = "tasks") -> list[dict[str, Any]]:
    if not isinstance(tasks, list):
        LOGGER.warning("task_state: skipping malformed %s list for event id=%s", context, event_id)
        return []
    by_id: dict[int, dict[str, Any]] = {}
    for item in tasks:
        normalized = _normalize_task_snapshot(item, event_id=event_id, context=context)
        if normalized is None:
            continue
        by_id[normalized["id"]] = normalized
    return [copy.deepcopy(by_id[key]) for key in sorted(by_id)]


def _projection_from_tasks(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    max_id = max((task["id"] for task in tasks), default=0)
    return {"tasks": [copy.deepcopy(task) for task in tasks], "next_id": max_id + 1 if max_id else 1}


def normalize_tasks_projection(data: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(data, dict):
        return _deepcopy_default_tasks(), False
    normalized_tasks = _normalized_tasks_from_iterable(data.get("tasks", []), context="projection task")
    projection = _projection_from_tasks(normalized_tasks)
    raw_next_id = data.get("next_id")
    try:
        parsed_next_id = int(raw_next_id)
    except (TypeError, ValueError):
        parsed_next_id = projection["next_id"]
    projection["next_id"] = max(projection["next_id"], parsed_next_id, 1)
    return projection, True


def load_tasks_projection(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _deepcopy_default_tasks()
    except json.JSONDecodeError:
        return _deepcopy_default_tasks()
    projection, _ = normalize_tasks_projection(raw)
    return projection


def load_tasks_projection_for_seed(path: Path) -> tuple[dict[str, Any] | None, bool]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, False
    except json.JSONDecodeError as exc:
        LOGGER.warning("task_state: legacy tasks seed skipped, malformed tasks.json: %s", exc)
        return None, False
    projection, valid = normalize_tasks_projection(raw)
    if not valid:
        LOGGER.warning("task_state: legacy tasks seed skipped, tasks.json shape is invalid")
        return None, False
    return projection, True


def save_tasks_projection(path: Path, projection: dict[str, Any]) -> None:
    atomic_write_json(path, projection)


def rebuild_task_summary_from_projection(projection: dict[str, Any]) -> dict[str, Any]:
    tasks = projection.get("tasks", [])
    pending = 0
    in_progress = 0
    done = 0
    last_created_at: str | None = None
    for task in tasks:
        status = str(task.get("status") or "pending")
        if status == "pending":
            pending += 1
        if status in {"assigned", "in_progress"}:
            in_progress += 1
        if status == "done":
            done += 1
        created_at = task.get("created_at")
        if isinstance(created_at, str) and created_at and (
            last_created_at is None or created_at > last_created_at
        ):
            last_created_at = created_at
    return {
        "total": len(tasks),
        "pending": pending,
        "in_progress": in_progress,
        "done": done,
        "last_created_at": last_created_at,
    }


def apply_task_event(projection: dict[str, Any], event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    if event_type not in TASK_REPLAY_EVENT_TYPES:
        return False

    tasks = projection.setdefault("tasks", [])
    event_id = event.get("id")

    if event_type == "tasks_cleared":
        tasks.clear()
        projection["next_id"] = 1
        return True

    if event_type in {"task_created", "task_updated"}:
        normalized = _normalize_task_snapshot(
            event.get("task"),
            event_id=int(event_id) if isinstance(event_id, int) else None,
            context=event_type,
        )
        if normalized is None:
            return False
        for index, existing in enumerate(tasks):
            if existing.get("id") == normalized["id"]:
                tasks[index] = normalized
                break
        else:
            tasks.append(normalized)
        tasks.sort(key=lambda item: int(item.get("id", 0)))
        projection["next_id"] = max((int(item["id"]) for item in tasks), default=0) + 1 if tasks else 1
        return True

    updated_tasks = _normalized_tasks_from_iterable(
        event.get("tasks"),
        event_id=int(event_id) if isinstance(event_id, int) else None,
        context=event_type,
    )
    if not updated_tasks:
        return False
    by_id = {int(task["id"]): copy.deepcopy(task) for task in tasks if isinstance(task, dict) and "id" in task}
    for task in updated_tasks:
        by_id[int(task["id"])] = task
    projection["tasks"] = [copy.deepcopy(by_id[key]) for key in sorted(by_id)]
    projection["next_id"] = max(by_id, default=0) + 1 if by_id else 1
    return True


def rebuild_tasks_projection_from_events(event_store: EventStore) -> dict[str, Any]:
    projection = _deepcopy_default_tasks()
    after_id = 0
    while True:
        batch = event_store.list_events(after_id=after_id, limit=1000)
        if not batch:
            break
        for event in batch:
            apply_task_event(projection, event)
        after_id = int(batch[-1]["id"])
    projection["tasks"] = _normalized_tasks_from_iterable(projection.get("tasks", []), context="rebuild task")
    projection["next_id"] = max((task["id"] for task in projection["tasks"]), default=0) + 1 if projection["tasks"] else 1
    return projection


def count_task_lifecycle_events(event_store: EventStore) -> int:
    return event_store.count_events(TASK_REPLAY_EVENT_TYPES)


def seed_legacy_tasks_if_needed(event_store: EventStore, tasks_path: Path) -> int:
    if count_task_lifecycle_events(event_store) > 0:
        return 0

    projection, valid = load_tasks_projection_for_seed(tasks_path)
    if not valid or projection is None:
        return 0
    tasks = projection.get("tasks", [])
    if not tasks:
        return 0

    seed_events = [
        {
            "type": "task_created",
            "ts": task.get("created_at") or utc_now(),
            "task_id": task["id"],
            "status": task.get("status"),
            "task": copy.deepcopy(task),
        }
        for task in tasks
    ]
    event_store.append_events(seed_events)
    LOGGER.info("task_state: seeded %s legacy task event(s) from %s", len(seed_events), tasks_path)
    return len(seed_events)


def event_for_callback(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    payload["event_id"] = event.get("id")
    if payload.get("type") == "tasks_bulk_updated":
        payload["type"] = "tasks_updated"
    return payload


def _build_new_task(
    *,
    task_id: int,
    title: str,
    task_type: str,
    priority: str,
    assigned_to: list[str] | None,
    source_message: str,
    now: str,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": title[:200],
        "type": task_type,
        "status": "assigned" if assigned_to else "pending",
        "priority": priority,
        "assigned_to": list(assigned_to or []),
        "source_message": source_message[:500],
        "created_at": now,
        "completed_at": None,
        "tokens_spent": 0,
    }


class TaskStateManager:
    """Single shared task command path used by relay and supervisor."""

    def __init__(
        self,
        *,
        event_store: EventStore,
        tasks_path: Path,
        state_store: Any | None = None,
        event_callback: EventCallback | None = None,
    ) -> None:
        self.event_store = event_store
        self.tasks_path = Path(tasks_path)
        self.state_store = state_store
        self.event_callback = event_callback
        self._lock = threading.RLock()
        self._projection = _deepcopy_default_tasks()

    def attach_state_store(self, state_store: Any | None) -> None:
        self.state_store = state_store

    def set_event_callback(self, event_callback: EventCallback | None) -> None:
        self.event_callback = event_callback

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._projection)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return rebuild_task_summary_from_projection(self._projection)

    def list_tasks(self, status_filter: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            tasks = [copy.deepcopy(task) for task in self._projection.get("tasks", [])]
        if status_filter:
            tasks = [task for task in tasks if task.get("status") == status_filter]
        return tasks

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            for task in self._projection.get("tasks", []):
                if task.get("id") == task_id:
                    return copy.deepcopy(task)
        return None

    def rebuild_from_events(self) -> dict[str, Any]:
        with self._lock:
            seeded = seed_legacy_tasks_if_needed(self.event_store, self.tasks_path)
            self._projection = rebuild_tasks_projection_from_events(self.event_store)
            self._flush_projection_locked()
            return {"seeded": seeded, "projection": copy.deepcopy(self._projection)}

    def create_task_command(
        self,
        *,
        title: str,
        task_type: str = "general",
        priority: str = "normal",
        assigned_to: list[str] | None = None,
        source_message: str = "",
    ) -> dict[str, Any]:
        title = str(title or "").strip()
        if not title:
            raise ValueError("title is required")

        callback_payload: dict[str, Any] | None = None
        with self._lock:
            now = utc_now()
            task = _build_new_task(
                task_id=int(self._projection.get("next_id") or 1),
                title=title,
                task_type=str(task_type or "general"),
                priority=str(priority or "normal"),
                assigned_to=_normalize_assigned_to(assigned_to),
                source_message=str(source_message or ""),
                now=now,
            )
            stored = self.event_store.append_event(
                {
                    "type": "task_created",
                    "ts": now,
                    "task_id": task["id"],
                    "status": task["status"],
                    "task": copy.deepcopy(task),
                }
            )
            apply_task_event(self._projection, stored)
            self._flush_projection_locked()
            callback_payload = event_for_callback(stored)

        if self.event_callback is not None and callback_payload is not None:
            self.event_callback(callback_payload)
        return task

    def update_task_command(
        self,
        task_id: int,
        *,
        status: str | object = UNCHANGED,
        assigned_to: list[str] | object = UNCHANGED,
        priority: str | object = UNCHANGED,
    ) -> dict[str, Any] | None:
        callback_payload: dict[str, Any] | None = None
        updated_task: dict[str, Any] | None = None
        with self._lock:
            current = self.get_task(task_id)
            if current is None:
                return None

            changed = False
            if status is not UNCHANGED:
                new_status = str(status or "").strip()
                if new_status not in TASK_STATUSES:
                    raise ValueError(f"invalid task status: {new_status}")
                if current.get("status") != new_status:
                    current["status"] = new_status
                    if new_status == "done":
                        current["completed_at"] = utc_now()
                    changed = True
            if assigned_to is not UNCHANGED:
                normalized_assigned_to = _normalize_assigned_to(assigned_to)
                if current.get("assigned_to") != normalized_assigned_to:
                    current["assigned_to"] = normalized_assigned_to
                    changed = True
            if priority is not UNCHANGED:
                new_priority = str(priority or "normal")
                if current.get("priority") != new_priority:
                    current["priority"] = new_priority
                    changed = True

            if not changed:
                return current

            stored = self.event_store.append_event(
                {
                    "type": "task_updated",
                    "ts": utc_now(),
                    "task_id": current["id"],
                    "status": current.get("status"),
                    "task": copy.deepcopy(current),
                }
            )
            apply_task_event(self._projection, stored)
            self._flush_projection_locked()
            callback_payload = event_for_callback(stored)
            updated_task = current

        if self.event_callback is not None and callback_payload is not None:
            self.event_callback(callback_payload)
        return updated_task

    def bulk_update_tasks_command(
        self,
        new_status: str,
        *,
        task_ids: list[int] | None = None,
        assigned_to: list[str] | object = UNCHANGED,
    ) -> list[dict[str, Any]]:
        new_status = str(new_status or "").strip()
        if new_status not in TASK_STATUSES:
            raise ValueError(f"invalid task status: {new_status}")

        callback_payload: dict[str, Any] | None = None
        updated_tasks: list[dict[str, Any]] = []
        selected_ids = None if task_ids is None else {int(task_id) for task_id in task_ids}
        with self._lock:
            current_tasks = [copy.deepcopy(task) for task in self._projection.get("tasks", [])]
            completed_at = utc_now() if new_status == "done" else None
            for task in current_tasks:
                if selected_ids is not None and int(task["id"]) not in selected_ids:
                    continue
                changed = False
                if task.get("status") != new_status:
                    task["status"] = new_status
                    if completed_at is not None:
                        task["completed_at"] = completed_at
                    changed = True
                if assigned_to is not UNCHANGED:
                    normalized_assigned_to = _normalize_assigned_to(assigned_to)
                    if task.get("assigned_to") != normalized_assigned_to:
                        task["assigned_to"] = normalized_assigned_to
                        changed = True
                if changed:
                    updated_tasks.append(task)

            if not updated_tasks:
                return []

            stored = self.event_store.append_event(
                {
                    "type": "tasks_bulk_updated",
                    "ts": utc_now(),
                    "status": new_status,
                    "new_status": new_status,
                    "task_ids": [task["id"] for task in updated_tasks],
                    "tasks": copy.deepcopy(updated_tasks),
                }
            )
            apply_task_event(self._projection, stored)
            self._flush_projection_locked()
            callback_payload = event_for_callback(stored)

        if self.event_callback is not None and callback_payload is not None:
            self.event_callback(callback_payload)
        return updated_tasks

    def clear_tasks_command(self) -> int:
        callback_payload: dict[str, Any] | None = None
        cleared_count = 0
        with self._lock:
            cleared_count = len(self._projection.get("tasks", []))
            if cleared_count == 0:
                return 0
            stored = self.event_store.append_event({"type": "tasks_cleared", "ts": utc_now(), "tasks": []})
            apply_task_event(self._projection, stored)
            self._flush_projection_locked()
            callback_payload = event_for_callback(stored)

        if self.event_callback is not None and callback_payload is not None:
            self.event_callback(callback_payload)
        return cleared_count

    def _flush_projection_locked(self) -> None:
        try:
            save_tasks_projection(self.tasks_path, self._projection)
        except Exception as exc:
            LOGGER.warning("task_state: failed to flush tasks projection %s: %s", self.tasks_path, exc)

        if self.state_store is None:
            return
        try:
            self.state_store.patch_tasks_summary(rebuild_task_summary_from_projection(self._projection))
        except Exception as exc:
            LOGGER.warning("task_state: failed to flush task summary to state.json: %s", exc)
