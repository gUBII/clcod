# clcod

`clcod` is the local control plane for a shared multi-agent room. It owns the
web UI, relay, tmux mirrors, workspace/project lock, and the task/event spine.

This repository is the orchestrator, not one of the long-running room agents.
If you are editing code here, read [docs/contributor-reality-check.md](docs/contributor-reality-check.md)
first for the maintainer/worker framing.

## Current Truths

- `events.db` is the durable source of truth for task lifecycle changes.
- `tasks.json` is a derived compatibility projection, not business truth.
- `state.json` is a derived materialized runtime view.
- Startup task recovery uses event replay, with one-time seeding from legacy
  `tasks.json` only when task lifecycle events do not yet exist.
- The workspace manager / project lock decides the active `work_dir` for room
  agents.
- tmux is a mirror/debug surface. It is not the source of truth.

## Snapshot

![Current local dashboard snapshot](docs/currentsnapshot.png)

## Docs

- [docs/architecture.md](docs/architecture.md) — runtime architecture, state
  ownership, task replay model, HTTP/SSE surface.
- [docs/operations.md](docs/operations.md) — start/stop, PM2, workspace
  manager, runtime files, troubleshooting.
- [docs/contributor-reality-check.md](docs/contributor-reality-check.md) —
  maintainer orientation for people working inside this repo.

Historical notes:

- [currentplan.md](currentplan.md) — archival planning snapshot, not runtime
  truth.
- [nextupgrade.md](nextupgrade.md) — backlog/context note, not runtime truth.

## Quick Start

Run directly:

```bash
bash start.sh
```

Open:

```text
http://127.0.0.1:4173
```

Stop:

```bash
bash stop.sh
```

Attach to tmux:

```bash
tmux attach -t triagent
```

Join the room from another terminal:

```bash
python3 join.py --config ./config.json --name "$USER"
```

## PM2

For repo-local service management, see [docs/operations.md](docs/operations.md).
The configured PM2 app name is `clcod-4173`.

## Source Priority

When docs disagree, trust sources in this order:

1. Running code and tests
2. [docs/architecture.md](docs/architecture.md)
3. repo policy files such as [AGENTS.md](AGENTS.md)
4. Historical planning notes

## License

MIT. See [LICENSE](LICENSE).
