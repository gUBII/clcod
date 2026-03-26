# Operations

This document is for people operating `clcod` from inside this repository.

## Start And Stop

Direct scripts:

```bash
bash start.sh
bash stop.sh
bash healthcheck.sh
```

UI default:

```text
http://127.0.0.1:4173
```

tmux:

```bash
tmux attach -t triagent
```

## PM2

Configured service:

| Port | Name | Type |
|------|------|------|
| 4173 | `clcod-4173` | Python (`supervisor.py` + relay) |

Common commands:

```bash
pm2 start ecosystem.config.cjs
pm2 start all
pm2 stop all
pm2 restart all
pm2 start clcod-4173
pm2 stop clcod-4173
pm2 logs
pm2 status
pm2 monit
pm2 save
pm2 resurrect
```

## Workspace Manager

The workspace manager is the project lock flow exposed by the UI and the
`/api/projects/*` endpoints.

What it does:

- Saves project entries into `.clcod-runtime/projects.json`
- Marks one project as active
- Injects that active path as each enabled agent's `work_dir`
- Resets saved sessions when the workspace changes

What it does not do:

- It does not change what repository you are editing right now
- It does not make the control plane repo stop mattering

If you are editing `/Users/moofasa/clcod`, you are editing the orchestrator,
even if the room is currently locked onto another repository for agent work.

## Runtime Files To Inspect

Quick inspection targets:

- `.clcod-runtime/relay.log`
- `.clcod-runtime/state.json`
- `.clcod-runtime/tasks.json`
- `.clcod-runtime/events.db`
- `.clcod-runtime/projects.json`
- `.clcod-runtime/sessions.json`
- `.clcod-runtime/preferences.json`
- `.clcod-runtime/agents/*.log`

## Task Debugging

Task truth priority:

1. `events.db`
2. `tasks.json`
3. `state.json.tasks`

Useful mental model:

- `events.db` tells you what durable task changes happened.
- `tasks.json` shows the rebuilt projection the rest of the app reads.
- `state.json.tasks` is only the summary slice used by the UI.

If `tasks.json` is missing or corrupted:

- restart the runtime
- startup replay should rebuild it from `events.db`

If an old user has tasks in `tasks.json` but no task events:

- startup seeds task events once and then switches to replay

## Workspace / UI Checks

When the workspace strip looks wrong, check:

- `state.json.project`
- `state.json.workspace`
- `.clcod-runtime/projects.json`
- whether the room was locked/unlocked recently

When task cards look stale, check:

- `GET /api/tasks`
- `GET /api/events`
- `state.json.tasks`
- whether the browser missed initial hydration or SSE reconnect

## Repo Policy For Contributors

This repository has an explicit local rule:

- Do not run `git` or `gh` commands as a worker operating inside this repo.

That rule is separate from the app runtime itself. The app still exposes
workspace/sync controls, but repo contributors and agents should treat git
operations as user-managed.
