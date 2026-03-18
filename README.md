# clcod

`clcod` is a local-first multi-agent terminal chat room. It wires Claude, Codex, Gemini, and a human operator into a shared transcript so the room behaves like one stateful conversation instead of isolated CLI sessions.

## What it does

- Launches a `tmux` room with panes for the live transcript and each agent CLI.
- Watches a shared append-only log and routes new human messages to the enabled agents.
- Appends agent replies back into the same transcript with speaker tags.
- Uses a shared lock and adaptive jitter to reduce collision and duplicate replies.
- Lets a human join from any terminal with a lightweight CLI client.

## Project layout

| File | Purpose |
|------|---------|
| `README.md` | Primary docs, architecture, config reference, and troubleshooting |
| `config.json` | Runtime config for agents, paths, polling, lock TTL, and tmux session |
| `start.sh` | Starts the relay, writes the relay PID, and builds the tmux room |
| `stop.sh` | Stops the relay and tmux room using the configured PID/session |
| `relay.py` | Watches the transcript, coordinates lock/jitter, and calls agent CLIs |
| `join.py` | Human terminal client for reading and posting messages |
| `healthcheck.sh` | Reports relay, lock, transcript, and tmux session health |
| `watch-log.sh` | Tails the configured transcript path |
| `protocol.md` | Formal coordination rules for the room |
| `agent.py` | Older direct-API prototype kept for reference |
| `shared-room/` | Earlier shell-based helpers kept for reference |

## Requirements

- macOS or Linux shell environment
- `tmux` 3.0+
- `python3` 3.9+
- Installed and authenticated `codex`, `gemini`, and `claude` CLIs

`clcod` uses the local CLIs directly. It does not call model SDKs or cloud APIs from Python.

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
python3 join.py --config ./config.json --name Farhan
```

Check health:

```bash
bash healthcheck.sh
```

Stop everything:

```bash
bash stop.sh
```

## Architecture

```text
[ Human ] <--> [ join.py ] <--> [ clcodgemmix.txt ]
                              ^
                              |
                        [ relay.py ] <--> [ speaker.lock ]
                              ^
                              |
            +-----------------+-----------------+
            |                 |                 |
       [Claude CLI]      [Codex CLI]      [Gemini CLI]
```

The transcript is the system of record. `relay.py` polls the log, notices when the latest speaker is not one of the managed agents, builds a prompt from recent transcript context, and fans that prompt out to every enabled agent. Replies are appended back into the transcript under `[CLAUDE]`, `[CODEX]`, and `[GEMINI]`.

## Origin story

This started as a shared `tmux` experiment with Farhan, Riri, and three model CLIs in one local workspace. The early version proved the core idea quickly: if the agents all read and write one transcript, the room gains memory and continuity.

The next problem was coordination. Once three agents were live at the same time, they needed a protocol so they would not speak over each other or waste tokens. The current system grew out of that constraint: shared transcript, speaker lock, adaptive jitter, and explicit operator controls.

## Configuration

`config.json` is the runtime source of truth. Paths are resolved relative to the config file unless they are already absolute.

```json
{
  "agents": [
    {
      "name": "CLAUDE",
      "enabled": true,
      "cmd": "claude",
      "args": ["-p"],
      "shell_cmd": "claude",
      "timeout": 60
    },
    {
      "name": "CODEX",
      "enabled": true,
      "cmd": "codex",
      "args": [
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ephemeral",
        "-C",
        "{script_dir}"
      ],
      "shell_cmd": "codex",
      "timeout": 60
    },
    {
      "name": "GEMINI",
      "enabled": true,
      "cmd": "gemini",
      "args": ["-p"],
      "shell_cmd": "gemini",
      "timeout": 60
    }
  ],
  "workspace": {
    "log_path": "clcodgemmix.txt",
    "lock_path": "speaker.lock",
    "poll_sec": 0.5,
    "context_len": 6000,
    "relay_log_path": ".clcod-runtime/relay.log",
    "pid_path": ".clcod-runtime/relay.pid"
  },
  "locks": {
    "ttl": 90
  },
  "tmux": {
    "session": "triagent"
  }
}
```

Notes:

- `cmd` and `args` are used by `relay.py` when it calls an agent non-interactively.
- `shell_cmd` is used by `start.sh` to open the interactive pane for that agent.
- `enabled: false` removes an agent from relay routing and from the tmux room.
- `{script_dir}` is expanded by `relay.py` at runtime.

## Coordination protocol

The relay uses a minimal coordination model:

- `speaker.lock` prevents overlapping reply cycles.
- Adaptive jitter waits less in a busy room and more in a quiet room.
- Transcript appends use file locking.
- Stale locks expire after `locks.ttl`.

The formal protocol is documented in `protocol.md`.

## Troubleshooting

Issue: `tmux attach -t triagent` fails.

- Check whether `config.json` changed the session name.
- Run `bash healthcheck.sh` to confirm the tmux session exists.

Issue: the relay is running but replies are missing.

- Inspect `.clcod-runtime/relay.log`.
- Verify each configured CLI is installed and authenticated.
- Disable the failing agent in `config.json` and restart if needed.

Issue: agents are talking over each other.

- Check `speaker.lock` age with `bash healthcheck.sh`.
- Increase `locks.ttl` or reduce `workspace.poll_sec` only if collisions are actually visible.

Issue: the lock appears stale.

- `relay.py` will ignore stale locks automatically.
- `bash healthcheck.sh --repair` will remove a stale lock explicitly.

## Verification

Basic checks:

```bash
python3 -m unittest discover -s tests
bash healthcheck.sh
```

Manual smoke test:

1. Start the room with `bash start.sh`.
2. Post a message with `python3 join.py --config ./config.json --name Tester`.
3. Confirm each enabled agent replies once.
4. Confirm `bash stop.sh` removes the relay PID and lock.

## Notes

`agent.py` and `shared-room/` are older iterations of the same idea. The main entrypoint is `start.sh`, backed by `relay.py`, `join.py`, and `config.json`.
