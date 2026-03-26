# Architecture

This document is the current architecture reference for `clcod`.

Historical planning files such as `currentplan.md` and `nextupgrade.md` are
useful context, but they are not the runtime spec.

## System Shape

`clcod` is a local-first control plane with two user-visible surfaces:

- The local web app is the primary operator surface.
- tmux is a mirror/debug surface owned by the supervisor.

Core runtime pieces:

- `supervisor.py` owns the HTTP server, SSE fanout, tmux layout, workspace
  manager, and runtime materialized state.
- `relay.py` owns transcript persistence, dispatch routing, dispatcher calls,
  and agent invocation.
- `dispatcher.py` classifies room messages before they are routed to cloud
  agents.
- `event_store.py` owns the SQLite event spine and dispatch queue.
- `task_state.py` owns task replay, task projections, and the shared event-first
  task command path.

## Source Of Truth

### Durable task state

Task state is intentionally normalized:

- `.clcod-runtime/events.db` is the single durable source of truth for
  state-changing task operations.
- `.clcod-runtime/tasks.json` is a derived compatibility projection.
- `.clcod-runtime/state.json` contains a derived `tasks` summary section.

Task mutations follow this order:

1. Validate input.
2. Build the post-change task snapshot payload.
3. Append the task event to `events.db`.
4. Apply the reducer to the in-memory task projection.
5. Best-effort flush `tasks.json` and the `state.json.tasks` summary.

If projection writes fail after the SQLite commit, the event is kept and the
next startup replay repairs the projections.

### Volatile runtime state

The following remain runtime-only/materialized state:

- tmux pane targets and mirror views
- live queue counters
- routing activity
- per-agent live process metadata
- transient UI-facing health state

## Task Replay Model

Task replay lives in `task_state.py`.

Replay behavior:

- Events are processed strictly in `id ASC` order.
- Unrelated events are ignored.
- Malformed task payloads are logged and skipped.
- Integer task IDs are preserved.
- `next_id` is rebuilt as `max(task.id) + 1`, or `1` when empty.

Task lifecycle events:

- `task_created`
- `task_updated`
- `tasks_bulk_updated`
- `tasks_cleared`

External SSE compatibility is preserved:

- Stored `tasks_bulk_updated` events are mapped to `tasks_updated` for SSE
  catch-up and live broadcasts.

### Legacy bootstrap

On startup, if there are no task lifecycle events in `events.db` but a valid
legacy `tasks.json` exists, the runtime seeds one `task_created` snapshot event
per task and then switches to normal replay.

That seeding is one-time bootstrap behavior, not an ongoing dual-truth mode.

## Workspace Manager

The workspace manager is the project lock flow in `supervisor.py`.

- `projects.json` stores saved project entries and the active lock.
- A locked project sets each enabled agent's `work_dir`.
- Unlocking returns agents to the home repo.
- The active project path is reflected in `state.json` and the UI workspace
  strip.

Important implication:

- The room may currently be pointed at another repository, but editing
  `/Users/moofasa/clcod` means you are changing the orchestrator itself.

## Runtime Files

| Path | Role |
|------|------|
| `clcodgemmix.txt` | Append-only room transcript |
| `.clcod-runtime/events.db` | Durable event spine and dispatch queue |
| `.clcod-runtime/tasks.json` | Derived task projection |
| `.clcod-runtime/state.json` | Derived runtime/materialized view |
| `.clcod-runtime/projects.json` | Workspace manager / project lock state |
| `.clcod-runtime/preferences.json` | Per-agent model/effort selections |
| `.clcod-runtime/sessions.json` | Saved resumable session IDs |
| `.clcod-runtime/relay.log` | Runtime log for supervisor/relay |
| `.clcod-runtime/agents/*.log` | Raw agent IO logs for mirror panes |
| `.clcod-runtime/room.sock` | Unix socket used by room submitters |
| `.clcod-runtime/archives/` | Transcript archive outputs |
| `speaker.lock` | Legacy artifact/cleanup path; dispatch serialization now lives in `events.db` |

## HTTP Surface

Primary GET endpoints:

- `GET /api/state`
- `GET /api/transcript?limit=N`
- `GET /api/events`
- `GET /api/projects`
- `GET /api/tasks?status=<exact-status>`
- `GET /api/dispatcher/health`
- `GET /api/agents/<name>/logs?tail=N`

Primary POST endpoints:

- `POST /api/unlock`
- `POST /api/chat`
- `POST /api/agents/<name>/settings`
- `POST /api/agents/<name>/restart`
- `POST /api/compact`
- `POST /api/repo/pull`
- `POST /api/sleep`
- `POST /api/projects/lock`
- `POST /api/projects/unlock`
- `POST /api/tasks`
- `POST /api/tasks/<id>`

## SSE Surface

The UI receives live updates from `GET /api/events`.

Task-related SSE event names exposed to the browser:

- `task_created`
- `task_updated`
- `tasks_updated`
- `tasks_cleared`

Other important SSE event names:

- `init`
- `state_refresh`
- `relay_state`
- `agent_state`
- `transcript`
- `dispatcher`
- `route_state`
- `dispatch_queued`
- `dispatch_started`
- `dispatch_completed`
- `dispatch_failed`
- `dispatch_skipped`
- `transcript_compacted`
- `transcript_summary_inserted`

## Current Recent Shifts Reflected In Code

These are current code truths, not inferred commit history:

- Task state is now event-first and replay-based.
- Startup rebuild repairs or recreates `tasks.json` from `events.db`.
- Legacy users are migrated by one-time task seeding.
- Projection writes for `tasks.json` and `state.json` are atomic.
- Relay and supervisor now share one task mutation path.
