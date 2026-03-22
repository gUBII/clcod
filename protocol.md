# IACP

`clcod` uses a minimal Inter-Agent Coordination Protocol so the room can run autonomously without constant operator cleanup.

## 1. Speaker lock

- Lock file: `speaker.lock`
- Owner: the relay process claims the lock before dispatching a reply cycle
- Purpose: prevent overlapping relay cycles when the transcript changes rapidly
- Expiry: if the lock age exceeds `locks.ttl`, it is treated as stale

## 2. Adaptive jitter

- Busy room: wait about `0.5s`
- Medium room: wait about `1.0s`
- Quiet room: wait about `2.0s`

The relay derives jitter from recent transcript activity. This gives humans a small window to keep typing in a quiet room while still keeping the room responsive under load.

## 3. Transcript rules

- The transcript is append-only.
- Every entry is tagged with `[SPEAKER]`.
- Only the latest non-agent speaker should trigger a new relay cycle.
- Agent appends use file locking to avoid partial writes.

## 4. Recovery

- A stale lock does not block the next relay cycle.
- `stop.sh` removes the lock during shutdown.
- `healthcheck.sh --repair` can remove a stale lock explicitly.

## 5. Human override

- A human can post directly through `join.py` or the web UI socket.
- A human can stop the room with `bash stop.sh`.
- A human can inspect the raw transcript and relay log at any time.

## 6. Optional handoff footer

Replies may include a footer such as:

```text
[DECISION: Task Done | OWNER: None | BLOCKERS: None]
```

This is a convention, not a hard requirement. The relay does not parse it.

## 7. Dispatcher routing

Messages pass through `dispatcher.py` (an Ollama-backed evaluator) before any cloud agents are invoked.

The dispatcher classifies incoming human messages into one of three actions:
- **route:** The message is assigned to a specific subset of relevant agents based on context.
- **absorb:** The request is trivial (e.g. "thanks") or answerable directly by the local model. The dispatcher responds immediately; no cloud calls are made.
- **clarify:** The request is ambiguous. The local model asks a follow-up question directly without invoking cloud agents.

If Ollama is unavailable, the dispatcher fails safely and falls back to routing the message to **all** active cloud agents.

## 8. Room commands

The chat surface supports explicit commands for overriding the dispatcher and tracking tasks:

- `@CLAUDE`, `@CODEX`, `@GEMINI` — A hard mention bypasses the dispatcher entirely and routes the message directly to the named agent.
- `/task <title>` — Creates a new task in `tasks.json` and alerts the room.
- `/move #<id> <status>` — Transitions a specific task to `pending`, `in_progress`, or `done`.
- `/moveall <status>` — Bulk transitions all active tasks to the chosen status.
- `/clearall` — Removes all existing tasks.
