# clcod

`clcod` is a local-first multi-agent workspace that keeps Claude, Codex, Gemini, and a human operator inside one shared transcript while exposing an app-style control surface, real runtime state, and a truthful tmux debug mirror.

## Dashboard Snapshot

The current local dashboard is shown below. This snapshot reflects the engine-room control deck, live transcript panel, and the in-app room composer introduced in the latest update.

![Dashboard snapshot](docs/dashboard-snapshot.svg)

The current architecture is intentionally split into two surfaces:

- The local web app is the primary operator UI.
- The tmux room is a mirror and debug surface created by the supervisor.

That distinction matters. The system of record is no longer "whatever happens to be visible in tmux". The system of record is the supervisor runtime state plus the shared transcript.

## Core idea

Three model CLIs and one human share one append-only room log:

- Humans write into `clcodgemmix.txt` with `join.py`.
- The relay watches for new non-agent messages.
- Enabled agents are invoked non-interactively.
- Replies are appended back into the same transcript.
- The supervisor owns tmux mirrors, runtime state, and the local UI server.

The result is one room with memory, rather than three unrelated chats.

## What changed in this version

The previous version was tmux-first. It launched interactive panes and separately launched relay subprocesses. That produced a false surface: the pane often showed a fresh chat that was not the real subprocess handling the room message.

This version fixes that directionally:

- `supervisor.py` owns the runtime lifecycle.
- `relay.py` owns the non-interactive agent calls and session persistence.
- `.clcod-runtime/state.json` is the live status contract for the UI.
- tmux is built by the supervisor as a mirror/debug workspace.
- Resume panes are only used when a real session actually exists.
- Cold boot stays honest: agents start in log-mirror mode until they have a real resumable session.

## Requirements

- macOS or Linux
- `python3` 3.9+
- `tmux` 3.0+
- authenticated local CLIs for any enabled models:
  - `claude`
  - `codex`
  - `gemini`

No model SDK is used from Python. The supervisor calls the installed CLIs directly.

## Quick start

Start the system:

```bash
bash start.sh
```

Open the UI:

```bash
http://127.0.0.1:4173
```

Default local password:

```text
free
```

Override it:

```bash
export CLCOD_PASSWORD='your-password'
bash start.sh
```

Attach to the debug room:

```bash
tmux attach -t triagent
```

Join the room from another terminal:

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

## High-level architecture

```mermaid
flowchart LR
    subgraph HumanSurface["Operator Surface"]
        Browser["Local Browser<br/>Password Gate<br/>Engine Room<br/>Workspace"]
        Join["join.py<br/>Human terminal client"]
    end

    subgraph ControlPlane["Supervisor-Controlled Runtime"]
        Start["start.sh"]
        Stop["stop.sh"]
        Health["healthcheck.sh"]
        Supervisor["supervisor.py<br/>process owner"]
        Relay["relay.py<br/>routing loop"]
        State[".clcod-runtime/state.json"]
        Sessions[".clcod-runtime/sessions.json"]
        RelayLog[".clcod-runtime/relay.log"]
        AgentLogs[".clcod-runtime/agents/*.log"]
        Lock["speaker.lock"]
        Transcript["clcodgemmix.txt"]
    end

    subgraph DebugSurface["Truthful Debug Surface"]
        Tmux["tmux session: triagent"]
        WatchPane["Pane 0<br/>watch-log.sh"]
        ClaudePane["Pane 1<br/>resume mirror or log mirror"]
        CodexPane["Pane 2<br/>resume mirror or log mirror"]
        GeminiPane["Pane 3<br/>log mirror"]
        RuntimeWin["Window 1<br/>tail relay.log"]
    end

    subgraph AgentCLI["Local Model CLIs"]
        Claude["claude CLI"]
        Codex["codex CLI"]
        Gemini["gemini CLI"]
    end

    Start --> Supervisor
    Stop --> Supervisor
    Health --> State
    Browser -->|GET / POST| Supervisor
    Join --> Transcript
    Supervisor --> State
    Supervisor --> Tmux
    Supervisor --> Relay
    Relay --> Lock
    Relay --> Transcript
    Relay --> Sessions
    Relay --> RelayLog
    Relay --> AgentLogs
    Relay --> Claude
    Relay --> Codex
    Relay --> Gemini
    Tmux --> WatchPane
    Tmux --> ClaudePane
    Tmux --> CodexPane
    Tmux --> GeminiPane
    Tmux --> RuntimeWin
    WatchPane --> Transcript
    ClaudePane --> AgentLogs
    ClaudePane -.resume if session exists.-> Claude
    CodexPane -.resume if session exists.-> Codex
    GeminiPane --> AgentLogs
    RuntimeWin --> RelayLog
    Supervisor --> Sessions
    Supervisor --> AgentLogs
```

## Detailed control-plane architecture

This diagram shows the actual ownership model in more detail.

```mermaid
flowchart TB
    subgraph Startup["Startup and Lifecycle"]
        S1["start.sh"]
        S2["stop.sh"]
        S3["healthcheck.sh"]
        S4["config.json"]
    end

    subgraph Runtime["Python Runtime"]
        P1["supervisor.py"]
        P2["StateStore"]
        P3["HTTP server"]
        P4["tmux manager"]
        P5["refresh loop"]
        P6["relay.run_relay(...)"]
    end

    subgraph Persistence["Runtime Persistence"]
        R1[".clcod-runtime/state.json"]
        R2[".clcod-runtime/sessions.json"]
        R3[".clcod-runtime/relay.log"]
        R4[".clcod-runtime/agents/claude.log"]
        R5[".clcod-runtime/agents/codex.log"]
        R6[".clcod-runtime/agents/gemini.log"]
        R7["speaker.lock"]
        R8["clcodgemmix.txt"]
    end

    subgraph UI["Local UI"]
        U1["GET /"]
        U2["POST /api/unlock"]
        U3["GET /api/state"]
        U4["GET /api/transcript"]
    end

    subgraph Mirrors["tmux Debug Mirrors"]
        M0["window 0: engines"]
        M1["pane 0: transcript tail"]
        M2["pane 1: Claude mirror"]
        M3["pane 2: Codex mirror"]
        M4["pane 3: Gemini mirror"]
        M5["window 1: runtime tail"]
    end

    subgraph Agents["CLI executables"]
        A1["claude"]
        A2["codex"]
        A3["gemini"]
    end

    S1 --> S4
    S1 --> P1
    S2 --> P1
    S3 --> R1
    S3 --> R8
    S3 --> M0

    P1 --> P2
    P1 --> P3
    P1 --> P4
    P1 --> P5
    P1 --> P6

    P2 --> R1
    P3 --> U1
    P3 --> U2
    P3 --> U3
    P3 --> U4

    P4 --> M0
    P4 --> M5
    P4 --> R2
    P4 --> R4
    P4 --> R5
    P4 --> R6
    P4 --> A1
    P4 --> A2

    P5 --> R8
    P5 --> R1
    P5 --> M0

    P6 --> R7
    P6 --> R8
    P6 --> R2
    P6 --> R3
    P6 --> R4
    P6 --> R5
    P6 --> R6
    P6 --> A1
    P6 --> A2
    P6 --> A3

    M0 --> M1
    M0 --> M2
    M0 --> M3
    M0 --> M4
    M5 --> R3
    M1 --> R8
    M2 --> R4
    M2 -.real resume when session exists.-> A1
    M3 --> R5
    M3 -.real resume when session exists.-> A2
    M4 --> R6
```

## Boot flow and engine-state semantics

The UI does not use fake timers. The "engine room" should reflect backend truth.

```mermaid
stateDiagram-v2
    [*] --> locked
    locked --> booting: POST /api/unlock
    booting --> starting: supervisor prepares runtime
    starting --> auth: tmux panes exist / mirrors assigned
    auth --> warming: relay active / agent call capability present
    warming --> ready: pane mirror is alive and runtime healthy
    starting --> error: startup failure
    auth --> error: pane creation or HTTP failure
    warming --> error: CLI timeout / missing binary / runtime exception
    error --> booting: clean restart

    state ready {
        [*] --> log_mirror
        log_mirror --> resumed_mirror: successful agent call creates real session
        resumed_mirror --> log_mirror: resume unavailable or mirror fails
    }
```

## Detailed message routing sequence

This is the core room behavior when a human posts a new message.

```mermaid
sequenceDiagram
    autonumber
    participant Human as Human Operator
    participant Join as join.py
    participant Transcript as clcodgemmix.txt
    participant Supervisor as supervisor.py
    participant Relay as relay.py
    participant Lock as speaker.lock
    participant Sessions as sessions.json
    participant Claude as claude CLI
    participant Codex as codex CLI
    participant Gemini as gemini CLI
    participant AgentLogs as agents/*.log
    participant State as state.json
    participant Browser as Browser UI

    Human->>Join: send message
    Join->>Transcript: append [FARHAN] block
    Relay->>Transcript: poll and detect new non-agent speaker
    Relay->>Lock: attempt lock acquisition
    alt lock unavailable and fresh
        Relay-->>Relay: wait for next poll interval
    else lock acquired
        Relay->>State: emit relay/agent state transitions
        par Claude route
            Relay->>Sessions: load known session id
            Relay->>Claude: invoke print mode or resume invoke
            Claude-->>Relay: stdout/stderr
            Relay->>AgentLogs: append Claude raw IO
            Relay->>Sessions: persist session id if newly established
            Relay->>Transcript: append [CLAUDE] reply
        and Codex route
            Relay->>Sessions: load known session id
            Relay->>Codex: invoke exec or exec resume
            Codex-->>Relay: stdout/stderr
            Relay->>AgentLogs: append Codex raw IO
            Relay->>Sessions: persist session id if extracted
            Relay->>Transcript: append [CODEX] reply
        and Gemini route
            Relay->>Gemini: invoke prompt mode
            Gemini-->>Relay: stdout/stderr
            Relay->>AgentLogs: append Gemini raw IO
            Relay->>Transcript: append [GEMINI] reply
        end
        Relay->>Lock: release lock
        Supervisor->>State: refresh transcript metadata, pane state, app phase
        Browser->>State: poll /api/state
        Browser->>Transcript: fetch /api/transcript
    end
```

## tmux layout

The supervisor creates tmux strictly as a mirror/debug surface.

```mermaid
flowchart TB
    subgraph Tmux["tmux session: triagent"]
        subgraph W0["window 0: engines"]
            P0["pane %0<br/>watch-log.sh<br/>tails transcript"]
            P1["pane %1<br/>agent 1 mirror"]
            P2["pane %2<br/>agent 2 mirror"]
            P3["pane %3<br/>agent 3 mirror"]
        end
        subgraph W1["window 1: runtime"]
            P4["tail -F .clcod-runtime/relay.log"]
        end
    end

    P0 -->|"source of human + agent room text"| T0["clcodgemmix.txt"]
    P1 -->|"resume mirror if possible, else tail agent log"| T1["agent mirror policy"]
    P2 -->|"resume mirror if possible, else tail agent log"| T1
    P3 -->|"tail agent log"| T1
    P4 -->|"supervisor and relay runtime logs"| T2["relay.log"]
```

Important consequences:

- A pane must never imply a live session that does not actually exist.
- Pane targets are stored as stable tmux pane IDs, not numeric indexes.
- `remain-on-exit` is enabled so a failed mirror cannot silently collapse the layout.
- Cold boot uses log mirrors until real resumable sessions are available.

## Runtime artifacts

The supervisor writes and consumes these files:

| Path | Purpose |
|------|---------|
| `clcodgemmix.txt` | shared append-only room transcript |
| `speaker.lock` | room-wide relay lock |
| `.clcod-runtime/state.json` | UI and health surface runtime contract |
| `.clcod-runtime/sessions.json` | persisted per-agent resumable session IDs |
| `.clcod-runtime/relay.log` | supervisor and relay operational log |
| `.clcod-runtime/agents/claude.log` | Claude raw IO mirror source |
| `.clcod-runtime/agents/codex.log` | Codex raw IO mirror source |
| `.clcod-runtime/agents/gemini.log` | Gemini raw IO mirror source |

## API surface

The local HTTP server lives inside `supervisor.py`.

### `GET /`

Serves the local app shell.

### `POST /api/unlock`

Payload:

```json
{
  "password": "free"
}
```

Effect:

- validates the local password
- creates an HTTP-only session cookie
- returns the current runtime snapshot

### `GET /api/state`

Before unlock:

```json
{
  "locked": true,
  "app": {
    "phase": "locked"
  }
}
```

After unlock:

- full runtime state
- app phase
- relay state
- tmux state
- per-agent mirror mode, mirror view, pane target, session ID, last error, last reply time

### `GET /api/transcript?limit=N`

Returns recent tagged transcript entries for the workspace view.

## Configuration reference

`config.json` is the runtime source of truth.

### Current shape

```json
{
  "agents": [
    {
      "name": "CLAUDE",
      "enabled": true,
      "cmd": "claude",
      "args": ["-p"],
      "invoke_resume_args": ["-p", "--session-id", "{session_id}"],
      "mirror_resume_args": ["--resume", "{session_id}"],
      "mirror_mode": "resume",
      "preseed_session_id": true,
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
        "-C",
        "{script_dir}"
      ],
      "invoke_resume_args": ["exec", "resume", "{session_id}"],
      "mirror_resume_args": ["resume", "--no-alt-screen", "-C", "{script_dir}", "{session_id}"],
      "mirror_mode": "resume",
      "preseed_session_id": false,
      "timeout": 60
    },
    {
      "name": "GEMINI",
      "enabled": true,
      "cmd": "gemini",
      "args": ["-p"],
      "mirror_mode": "log",
      "preseed_session_id": false,
      "timeout": 60
    }
  ],
  "workspace": {
    "log_path": "clcodgemmix.txt",
    "lock_path": "speaker.lock",
    "poll_sec": 0.5,
    "context_len": 6000,
    "relay_log_path": ".clcod-runtime/relay.log",
    "pid_path": ".clcod-runtime/supervisor.pid",
    "state_path": ".clcod-runtime/state.json",
    "sessions_path": ".clcod-runtime/sessions.json",
    "agent_logs_dir": ".clcod-runtime/agents"
  },
  "locks": {
    "ttl": 90
  },
  "tmux": {
    "session": "triagent"
  },
  "ui": {
    "host": "127.0.0.1",
    "port": 4173,
    "password_env": "CLCOD_PASSWORD",
    "password": "free",
    "open_browser": true
  }
}
```

### Semantics

#### `agents[].args`

Base non-interactive invocation arguments used before a resumable session exists.

#### `agents[].invoke_resume_args`

Arguments used by the relay when a real session ID is known and the agent supports resumed non-interactive invocation.

#### `agents[].mirror_resume_args`

Arguments used by the supervisor when the tmux pane should attach to the actual agent session instead of tailing a log.

#### `agents[].mirror_mode`

- `resume`: prefer a real resumed pane when safe and available
- `log`: always use log tailing

#### `agents[].preseed_session_id`

Controls whether the invoke path may create a deterministic session identifier for the first successful relay call. Boot-time tmux mirrors do not trust this value by itself.

#### `workspace.state_path`

The file the UI and `healthcheck.sh` read as the live runtime contract.

## File layout

| File | Purpose |
|------|---------|
| `start.sh` | boot entrypoint |
| `stop.sh` | shutdown entrypoint |
| `healthcheck.sh` | runtime health and stale-state reporting |
| `supervisor.py` | process owner, HTTP server, tmux manager, state writer |
| `relay.py` | transcript watcher and agent router |
| `join.py` | human CLI client |
| `watch-log.sh` | transcript tail helper used in tmux |
| `web/index.html` | local app shell |
| `web/app.js` | UI state polling, unlock flow, rendering |
| `web/styles.css` | engine-room visual language and animations |
| `tests/test_relay.py` | relay unit tests |
| `tests/test_supervisor.py` | supervisor unit tests |

## Health model

`healthcheck.sh` reports:

- supervisor PID presence and liveness
- stale lock detection
- transcript presence
- runtime state freshness
- app phase
- relay state
- per-agent state, mirror view, and pane target
- tmux session presence

Expected healthy output characteristics:

- app phase is `ready`
- relay state is `running`
- tmux session exists
- each enabled agent has a pane target
- `state.json` age is recent

## Verification

Static checks:

```bash
python3 -m py_compile relay.py supervisor.py join.py
bash -n start.sh stop.sh healthcheck.sh
```

Unit tests:

```bash
python3 -m unittest discover -s tests
```

Suggested manual smoke:

1. Run `bash start.sh`.
2. Open `http://127.0.0.1:4173`.
3. Unlock with `CLCOD_PASSWORD` or the configured fallback password.
4. Confirm `/api/state` shows `app.phase = ready`.
5. Confirm tmux exists with `tmux attach -t triagent`.
6. Post a human message with `join.py`.
7. Confirm enabled agents append replies into `clcodgemmix.txt`.
8. Confirm `.clcod-runtime/agents/*.log` update.
9. Confirm `bash healthcheck.sh` stays green.
10. Run `bash stop.sh`.

## Operational notes

- The browser UI is local-only convenience access control, not production authentication.
- The transcript remains the room's canonical conversational history.
- The tmux surface is for observability and debugging, not the source of truth.
- Resume mirrors are opportunistic. If an agent cannot safely resume, the system falls back to a log mirror.
- This repo still contains older artifacts such as `agent.py` and `shared-room/`; the active path is the supervisor + relay + web UI stack.

## Troubleshooting

### The app does not open

- Check `bash healthcheck.sh`.
- Check `.clcod-runtime/relay.log`.
- Verify nothing else is listening on the configured UI port.

### The UI is up but shows `locked`

- Call `POST /api/unlock` by using the form in the browser.
- Confirm `CLCOD_PASSWORD` matches what you expect.

### tmux exists but panes look wrong

- Check `state.json` pane targets and mirror views.
- Confirm the engine window has four panes: transcript plus three agent panes.
- Confirm failed mirrors are not being mistaken for live resumed sessions.

### Agents are not replying

- Check each agent binary is installed and authenticated.
- Inspect `.clcod-runtime/agents/*.log`.
- Inspect `.clcod-runtime/relay.log`.
- Disable failing agents in `config.json` and retry.

### A pane should be resumed but is still a log mirror

- That means no real session has been established yet, or resume failed and the supervisor fell back to the truthful mirror.
- Send a real room prompt first so the relay can create and persist the session.

## License

This project is licensed under the MIT License. See [LICENSE](/Users/moofasa/clcod/LICENSE).
