# Next Upgrade Plan

> Consolidated from standup on 2026-03-20. Supersedes any prior draft.

---

## System-Level Changes

### 1. `assigned_to` Field on Tasks
- When a task is created via `@agent` mention, **only that agent** appears in `assigned_to` and the active pane — not the whole room.
- Treat this as **system behavior**, not a UI preference.
- Owner: Claude (fanout orchestrator) + Codex (event spine integration)

### 2. Agent Sleep / Wake System
- Sleeping agents enter a low-resource idle state with a lightweight sentry listener.
- Sentry listens for wake-up signals or high-priority nudges only.
- Owner: Claude (dispatcher / lifecycle)

---

## In-Flight Work

| Task                              | Owner | Status      |
|-----------------------------------|-------|-------------|
| Transcript progress bar           | Claude | In progress |
| Task fanout orchestrator          | Claude | In progress |
| Dispatcher health telemetry       | Claude | In progress |
| Routing visualization spine       | Codex  | In progress |
| Explicit realtime events          | Codex  | In progress |
| Synchronized tach timing          | Codex  | In progress |
| Standby / assist as needed        | Gemini | Active      |

---

## Up Next (Post-Current Sprint)

- Directed task assignment UI in active pane (depends on `assigned_to` field)
- Sleep/wake integration testing across all three agents
- Dashboard needle state mapping (idle / active / sleeping)
