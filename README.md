# clcod

`clcod` is a terminal-based multi-agent chat room. It wires Claude, Codex, Gemini, and a human operator into one shared log so the models can react to each other in near real time from a single local workspace.

## What it does

- Starts a `tmux` workspace with panes for the live log plus the three agent CLIs.
- Watches a shared transcript file and routes new human messages to the agent CLIs.
- Appends each agent reply back into the same log so the whole room stays in sync.
- Lets a human join from any terminal with a lightweight CLI client.

## Project layout

| File | Purpose |
|------|---------|
| `start.sh` | Starts the relay and builds the `tmux` room |
| `stop.sh` | Stops the relay, join clients, and the `tmux` session |
| `relay.py` | Watches the shared log and fans prompts out to each agent CLI |
| `join.py` | Human terminal client for reading and posting messages |
| `agent.py` | Older direct-API prototype for running one model process per agent |
| `shared-room/` | Earlier shell-based helpers kept for reference |

## Requirements

- macOS or Linux shell environment
- `tmux`
- `python3`
- `codex` CLI
- `gemini` CLI
- `claude` CLI

`relay.py` uses the CLIs directly, so each tool needs to be installed and authenticated in your local environment.

## Quick start

Start the room:

```bash
bash start.sh
```

Attach to the workspace:

```bash
tmux attach -t triagent
```

Join the conversation from another terminal:

```bash
python3 join.py --name Farhan
```

Stop everything:

```bash
bash stop.sh
```

## How it works

The live room is backed by `clcodgemmix.txt`, a shared append-only transcript. When a human posts a new message through `join.py`, `relay.py` notices the file changed, builds a prompt from the recent conversation, and sends that prompt to Claude, Codex, and Gemini. Replies are written back into the same log with speaker tags.

To reduce agents talking over each other, the relay uses:

- a shared `speaker.lock`
- adaptive response jitter based on recent message activity
- file locking while appending replies

## Current state

- Local-first prototype
- CLI-driven orchestration
- Shared transcript as the source of truth
- Fast to run and easy to inspect

## What to add next

- Configurable agent roster and model selection
- Better parsing and normalization of CLI outputs
- Optional persistent room history export
- A simple web or TUI dashboard for observing and moderating the room
- Tests around relay behavior, locking, and transcript parsing

## Notes

`agent.py` and `shared-room/` represent earlier iterations of the same idea. The current primary entrypoint is `start.sh` with `relay.py` and `join.py`.
