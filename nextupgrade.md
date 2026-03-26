# Next Upgrade Plan

> Backlog/context note only.
> Do not treat this file as the live runtime spec.
> For current behavior, use `docs/architecture.md`, `docs/operations.md`, and
> the code/tests.



---

## System-Level Changes

### 1. `assigned_to` Field on Tasks
- When a task is created via `@agent` mention, **only that agent** appears in `assigned_to` and the active pane — not the whole room.
- Treat this as **system behavior**, not a UI preference.
- Owner: Core (fanout orchestrator) + Codex (event spine integration)

### 2. Agent Sleep / Wake System
- Sleeping agents enter a low-resource idle state with a lightweight sentry listener.
- Sentry listens for wake-up signals or high-priority nudges only.
- Owner: Core (dispatcher / lifecycle)

---

## Bug Fix Sprint — Completed 2026-03-24

| Issue | Fix | File |
|-------|-----|------|
| Lock release swallows errors | Only catch `FileNotFoundError`, propagate rest | `relay.py` |
| Advisory flock deadlock | `LOCK_NB` retry loop + `try/finally` unlock | `relay.py` |
| Dispatcher silent fallback | Exponential backoff retries, `fallback: true` flag | `dispatcher.py` |
| SSE queue memory leak | Subscriber cap (default 32), 503 on overflow | `supervisor.py` |
| No circuit breaker | `CircuitBreaker` class, half-open recovery, events emitted | `relay.py` |
| Context loading O(N) | `read_tail()` seeks to tail, skips full file read | `relay.py` |

## In-Flight Work

| Task                              | Owner | Status      |
|-----------------------------------|-------|-------------|
| Transcript progress bar           | Core  | In progress |
| Task fanout orchestrator (`assigned_to` field) | Core  | In progress |
| Routing visualization spine       | Core  | In progress (circuit breaker events now feed this) |
| Synchronized tach timing          | Core  | In progress |
| Standby / assist as needed        | Core  | Active      |

---

## Up Next (Post-Current Sprint)

- Directed task assignment UI in active pane (depends on `assigned_to` field)
- Sleep/wake integration testing across all three agents
- Dashboard needle state mapping (idle / active / sleeping)





# Deep Research Review and Modernization Blueprint for a Local‑First Multi‑Agent Orchestration Platform

## System snapshot and design intent

Based on your description, the system is aiming for a **local-first, inspectable, “everything is a log”** multi-agent environment where the orchestration layer stays lightweight and debuggable, and the UI can observe everything in real time.

At a component level, the current system looks like this:

- **Shared append-only transcript** acts as *both* message bus and (partial) state store.
- A **relay/dispatcher** (using a local LLM via Ollama) routes messages and decides what runs next. Ollama exposes a REST API with streaming responses (e.g., `/api/chat`) and explicitly supports streaming as a first-class behavior. 
- **External AI CLIs** (Claude/Codex/Gemini) are invoked as subprocesses.
- A **Python supervisor** maintains runtime state, handles loop control, and likely orchestrates subprocess lifecycles.
- **tmux** is used as a “mirror surface” for debugging/visualization. tmux itself is a **client-server terminal multiplexer**, with a server managing state and clients attaching via Unix-domain sockets. 
- A **Web UI** streams updates using **SSE**. SSE (EventSource) is a unidirectional server→client stream using `text/event-stream`. 
- There are higher-level affordances like **tasks, project locking, and agent configuration**.

This is a coherent “prototype-to-product” lineage: it uses the simplest primitives that deliver local-first behavior and great introspection. The question is where it breaks as it grows.

## Evaluation of the current architecture

### Bottlenecks and scaling limits

**The append-only transcript is doing too many jobs at once.**  
What you’re calling a “transcript as source of truth” is *functionally very close to event sourcing*, where state changes are recorded as events and state is rebuilt by replaying them. Martin Fowler's definition highlights this: record state changes as events, and rebuild derived state from the log, making the event store the principal source of truth.   
The problem is that a plain text transcript typically lacks the parts that make event sourcing scale safely: schema’d event types, explicit event IDs/offsets, consumer checkpoints, snapshotting, compaction, and invariants.

**Polling + appending is a throughput and latency killer.**  
A relay loop that polls a file to find new messages tends to accumulate edge cases: missed writes, partial writes, duplicated reads, and CPU overhead proportional to log size. This becomes visible when you scale agent count, token throughput, or concurrency.

**CLI invocation is an unavoidable drag on responsiveness and reliability.**  
Every “call model via CLI” tends to imply:
- process spawn cost per call
- parsing stdout/stderr as an API surface
- version drift and output format churn
- fragile error handling (exit codes vary; transient network errors look like “random text”)
- awkward streaming (you’re scraping partial stdout rather than receiving structured token events)  
Even if it works, it becomes a major operational burden once you run multiple agents concurrently.

**Single supervisor as a chokepoint.**  
A single Python supervisor is typically fine for local-first, but the more it does (routing, process management, serialization, state writes, UI fanout), the more it becomes:
- the main latency contributor (everything queues behind it)
- a single-point-of-failure
- hard to test because implicit state grows

**tmux mirroring does not scale as an observability substrate.**  
tmux is excellent for humans, but it’s not a machine-consumable telemetry pipeline. Also, tmux’s superpower is its own client-server architecture using a Unix socket.   
That’s great for manual inspection, but as the *primary* debugging surface it tends to produce “debugging-by-watching” instead of “debugging-by-querying” (filterable logs, trace spans, deterministic replay).

**SSE is good, but it constrains interaction patterns.**  
SSE/EventSource is explicitly **unidirectional** (server→client).   
For many agent UIs that’s fine: you can stream outputs via SSE and send commands via normal HTTP POST. But once you want richer real-time interaction (bi-directional control, multiplexing many streams, interactive debugging consoles), you’ll either bolt on additional channels or move to WebSockets.

### Fragility points

**Shared file concurrency and correctness.**  
A text file as a shared bus usually depends on file locks or careful discipline. Even when you “append-only,” correctness issues appear under:
- concurrent writers
- crash mid-write (truncated line / partial JSON)
- consumer restart (where do you resume? how do you guarantee exactly-once processing?)
- log growth (parsing becomes slower, compaction becomes necessary)

**Implicit state derived from transcript and supervisor memory diverges.**  
If the transcript is “truth” but the supervisor is also maintaining runtime state, these can drift on crash, partial replay, schema changes, or bugs. Without explicit projections and replay determinism, recovery becomes “best effort.”

**Backpressure is not explicit in the system.**  
When multiple agents stream tokens concurrently, you need a plan for:
- slowing producer(s) when consumers/UI can’t keep up
- bounding memory in queues/buffers
- prioritizing streams (foreground vs background agents)

SSE helps because it’s simple HTTP streaming, but buffering and slow clients still matter.

### Reliability, fault tolerance, and recovery

**Best-case: the transcript gives you crude durability.**  
If everything writes to the transcript before doing anything else, you get recoverability via replay—again, that’s event sourcing in spirit.   
But reliability is primarily limited by:
- lack of formal event schema + versioning
- no consumer checkpoint model
- no idempotency keys to prevent duplicate processing
- no systematic retries / poison-message handling

**A “real message system” gives you semantics you will reinvent otherwise.**  
For example:
- **Redis Streams** is literally positioned as an append-only log with richer consumption strategies (e.g., consumer groups). 
- Streams have explicit tracking of pending entries (messages delivered but not acked), inspectable via commands like `XINFO GROUPS` (shows pending length, lag) and `XPENDING` (pending entry management). 
- **NATS Core** gives best-effort pub/sub (**at-most-once** delivery), while **JetStream** adds persistence and stronger delivery semantics (at-least-once and even “exactly once” patterns). 

Crucially, JetStream’s documentation also points out a subtle durability detail: by default it does not immediately `fsync` every write (it uses a configurable `sync_interval`, default 2 minutes), which can matter for single-node power-loss durability.   
That’s the kind of operational nuance you don’t want to learn the hard way.

### Observability assessment

Your system has **excellent “visual observability”** (tmux + streaming UI). What it likely lacks is **structured, queryable observability**:

- **Tracing and correlation** across the supervisor → agent runner → provider call → tool call chain.
- **Causal linking** between events (this output token belongs to this agent run which belongs to this task).
- A clean **telemetry vocabulary** (events, spans, metrics).

OpenTelemetry is designed for this cross-cutting correlation: signals (traces/metrics/logs) share context propagation so you can correlate activity across process boundaries.   
If your system becomes multi-process (and it already is if you invoke CLIs), this matters quickly.

### Developer ergonomics and maintainability

What’s good today:
- Very low barrier to adding a new “agent” if it’s just another CLI.
- “You can see it working” thanks to tmux + SSE.
- The transcript is a built-in audit trail.

What gets hard over time:
- Changes become risky because the transcript is implicitly a public API (anything parsing it depends on its format).
- Bugs become nondeterministic (polling loops + concurrent writes + subprocess timing).
- Tests become integration-heavy (hard to unit-test “append to file and poll it later” deterministically).
- “State” means three things at once (log + supervisor memory + UI render state).

## Brutally direct critique of the experimental design

### What is clever

**A shared append-only transcript as a universal audit log is a legitimate idea.**  
It mirrors the core advantage of event sourcing: if you can replay, you can debug and recover. 

**Using tmux as a debug surface is pragmatic.**  
tmux is designed for persistent multi-pane workflows and detaching/reattaching sessions.   
For experimental systems, that’s a huge productivity boost.

**SSE streaming is a sane default for local-first UIs.**  
The WHATWG spec explicitly supports reconnection and defines `Last-Event-ID` behavior for resuming after disconnects, which is useful in “keep it simple” streaming dashboards.   
MDN also emphasizes that SSE is unidirectional, which aligns well with “observe everything” UIs. 

**Ollama as local dispatcher is practical.**  
It offers streaming and tool calling concepts in its API, and it’s designed to run locally with a REST surface. 

### What is fragile or hacky

**A text file is not a message bus. It’s a log you are pretending is a bus.**  
The moment you need acknowledgements, consumer offsets, or multiple independent consumers, you’re duct-taping semantics onto newline-delimited text.

**Polling loops don’t just waste CPU—they create correctness traps.**  
When you poll, you are always asking “did I miss something?” and “did I read it twice?” and “what if it was half written?”—forever.

**tmux is not an observability backend.**  
It’s a UI. tmux’s own architecture is a server + clients over Unix sockets.   
If you’re using it as a core system component, you’re coupling correctness to terminal presentation.

**CLI tools are not stable RPC interfaces.**  
They are inherently not designed to be invoked as a low-latency, long-running, structured protocol boundary.

### What will break at scale

“Scale” here means: more agents, bigger prompts, more tool calls, longer runs, more concurrent tasks, more UI sessions.

- The transcript grows without bound → startup and replay slow down unless you implement compaction/snapshotting (event sourcing problems you didn’t plan to own).
- Polling latencies add jitter and compound with concurrency.
- Multiple simultaneous CLI calls amplify process management fragility (timeouts, leaking subprocesses, deadlocks on stdout pipes).
- UI fanout becomes expensive if every token is broadcast naïvely.

### What will be hard to maintain

- Debugging correctness issues across file offsets, polling timing, and subprocess behavior.
- Evolving transcript format without breaking consumers.
- Adding robust failure handling (retries, poison messages, idempotency) without re-implementing message queue concepts.

### Top architectural risks

These are the five risks I’d treat as “address first” because they can become existential as usage increases.

**Risk: message correctness and replay safety**  
Without explicit event IDs, checkpoints, and idempotency, you’ll get duplicates, missed events, and “heisenbugs” during resume/replay.

**Risk: single-writer bottlenecks and write contention**  
If you move to SQLite (which you should for structured durability), remember that even in WAL mode you still only get **one writer at a time**. SQLite’s WAL docs are explicit: readers and writers can run concurrently, but “there can only be one writer at a time.”   
This is manageable with batching, but it needs an intentional write model.

**Risk: crash recovery without formal projections**  
Event sourcing works when replay deterministically reconstructs state.   
A free-form transcript + supervisor memory usually does not meet that bar.

**Risk: security boundary collapse as tools expand**  
Multi-agent systems that can read/write files and run code are naturally exposed to prompt injection and tool misuse. The meta-lesson is the same: **tool boundaries and permissions must be explicit and enforced**.

**Risk: observability debt**  
Without a trace/log correlation model, diagnosing “why did agent B do that?” becomes manual log spelunking. OpenTelemetry exists precisely to correlate signals across boundaries via context propagation. 

## Recommended modernized architecture

### The core change: split durable history from live routing

Your transcript is trying to be:
- the durable record
- the live bus
- the derived state store
- the UI stream source

In a more robust design, you should explicitly separate:

**Durable event store (append-only, schema’d)**  
Use event sourcing deliberately: store typed events, replayable, versioned. Fowler’s framing is the right mental model: record state changes as events and rebuild state from them. 

**Live message transport (fast, ephemeral or semi-durable)**  
Use a real IPC or broker semantics for “who should react to this event right now?”

This split alone removes most of the “fragile but clever” aspects while preserving the spirit and debuggability.

### Replace tmux mirroring with “structured observability plus optional terminals”

tmux is a fantastic *manual* view. Keep it as an optional developer tool, but make the primary debug surface:

- A **web “timeline”** of events (task created → agent spawned → prompt built → provider called → tokens streamed → tool called → error → retry).
- Per-agent **structured logs** and **run artifacts** stored with stable IDs.
- Optional **interactive terminal attach** for a worker/agent process (only when needed).

If you want the same “attach/detach” ergonomics, note that tmux achieves persistence via a server process and Unix socket clients.   
You can emulate this pattern by having **your orchestrator own PTYs** and expose them via the web UI, rather than outsourcing the whole debug UX to tmux.

### Replace transcript-based state with a local event store + projections

**Use SQLite (WAL mode) as the default local-first backbone.**  
SQLite WAL explains why it’s good for local-first apps: writers append to a WAL file and readers can continue on the prior snapshot; checkpoints move WAL content back into the DB.   
But be honest about its limit: only one writer can hold the write lock at a time.   
That’s usually fine if you:
- batch event writes (append events in chunks)
- avoid “chatty” transactions (don’t commit every token; commit per message chunk or time-slice)
- keep projections updated incrementally

**Store events as structured rows.**  
A practical schema pattern:

- `events(id, ts, stream_id, type, payload_json, causation_id, correlation_id, actor_id, seq)`
- `streams(stream_id, last_seq, metadata_json)`
- projections/materialized views:
  - `tasks`, `runs`, `agents`, `locks`, `artifacts`, `messages`

SQLite’s JSON functions are built-in by default since SQLite 3.38.0, which makes event payload storage/querying feasible without adding a new DB. 

### Replace CLI-based agent invocation with long-lived “agent runners” over RPC

The key idea: **stop invoking providers as “commands.” Start treating them as “services.”**

**Agent Runner (per provider or per agent type)**  
Run a long-lived process that:
- receives structured requests (prompt, config, tool schema)
- streams structured responses (tokens/events)
- exposes health, version, and capabilities endpoints

Use **gRPC** for this boundary if you want a strong contract and streaming semantics. gRPC explicitly supports bi-directional streaming and has built-in concerns like tracing, health checking, and auth in its ecosystem. 

If you want the simplest local IPC, you can run gRPC over localhost TCP; if you want tighter local security, consider Unix domain sockets (platform permitting). The core is: **make the provider boundary a contract, not stdout text.**

**Transitional design:** keep the CLI, but wrap it.  
In early migration, your runner can still call the provider CLI internally, but it should:
- normalize errors into structured codes
- normalize streaming into token events
- isolate CLI version changes behind a stable RPC

### Better IPC and orchestration mechanisms

There are two “good” directions, depending on how much you want to lean into multi-process.

**Option focused on simplicity (recommended baseline)**  
- One **Orchestrator daemon** owns routing decisions and state writes.
- Agent runners connect to orchestrator via gRPC streams.
- Orchestrator pushes updates to UI via SSE/WebSocket.
- Durable store is SQLite.

This avoids bringing in a broker and avoids distributed-system complexity while dramatically improving correctness.

**Option focused on scalability and decoupling (still local-first)**  
Add a lightweight broker:

- **NATS Core** for fast pub/sub (best-effort at-most-once).   
- Add **JetStream** only if you want broker-level persistence and replay.   

Or:
- **Redis Streams** as an append-only log with consumer groups and explicit pending/ack tracking.   

The broker option is helpful if you want:
- multiple independent consumers (metrics pipeline, UI pipeline, background compactor, etc.)
- mailbox per actor without everything funneled through one process

Given your “prefer simple” constraint, I’d start with the orchestrator-only baseline, then add NATS/Redis only when you can state a concrete need.

### AI orchestration improvements

#### Replace or enhance dispatcher logic

A local LLM dispatcher is a reasonable idea, but it needs *guardrails and determinism*.

Upgrade the dispatcher from “LLM decides what to do” to a **policy-based router**:

- Use deterministic routing rules first (agent capabilities, task type, constraints).
- Use a small classifier LLM only when rules cannot decide.
- Log the router’s decision as a first-class event with:
  - inputs (features), chosen route, confidence, fallback path
  - correlation ID for tracing

Since Ollama supports streaming and structured chat requests, it can still be your local dispatch model. 

#### Multi-agent coordination patterns and frameworks

You don’t need a heavy framework, but it’s useful to adopt known coordination patterns:

- **Actor model**: each agent is an actor with a mailbox; it processes one message at a time; supervision handles failure and restarts. Akka’s docs summarize the key benefits: encapsulation without locks and asynchronous message passing; supervision is a built-in theme.   
- **State-machine / graph workflows**: represent coordination as a graph of states/transitions.

If you want a library rather than building from scratch:
- **LangGraph** is explicitly positioned as a low-level orchestration runtime for long-running, stateful, streaming agents.   
- **AutoGen** (Microsoft) is a multi-agent conversation framework focused on composing conversable agents into patterns.   

Even if you don’t adopt them wholesale, they’re good references for features you’ll likely need (durable execution, streaming, human-in-the-loop). 

#### Memory, context, and local-first persistence

Treat “memory” as layered:

- **Run memory**: ephemeral scratch + token stream; kept for a single run.
- **Project memory**: durable facts, artifacts, summaries.
- **Global memory**: preferences, routing history, capability stats.

For local-first storage:
- Keep structured state in SQLite.
- For retrieval embeddings:
  - **sqlite-vec** is a vector search extension designed to “run anywhere SQLite runs” and store/query vectors in virtual tables.   
  - If you want a dedicated local vector DB, **Qdrant** can run locally via Docker and exposes REST + gRPC.   

For local-first sync (if you want multi-device eventually):
- The “local-first software” principles emphasize offline capability, user control, and collaboration without surrendering data ownership.   
- CRDT libraries like **Automerge** provide automatic merging of concurrent edits and are network-agnostic.   
- **Yjs** similarly describes itself as a high-performance CRDT that merges changes automatically.   

A pragmatic approach is:
- SQLite for your authoritative event store on one machine
- CRDT for any data you truly want to sync/merge across devices (e.g., notes, task boards, shared agent memory)

#### Tool and provider integration standardization

You currently integrate “many models × many tools” via custom glue and CLIs. Consider adopting **Model Context Protocol (MCP)** concepts as guidance, even if you don’t fully standardize on it immediately.

Anthropic describes MCP as an open protocol to standardize how apps provide context/tools to LLMs.   
The independent MCP spec site also shows the protocol is versioned and evolving, with “stable/legacy/draft” tracks.   

Whether you adopt MCP or not, the strategic idea is valuable: **a single tool interface** so your orchestrator doesn’t become a tangle of provider-specific glue.

## Technology recommendations

### Backend and orchestration runtime

Given you already have a Python supervisor, the fastest path to “modernized but not overcomplicated” is:

- **Python orchestrator daemon** (asyncio-based)
- **API server** in Python (FastAPI/Starlette style), colocated or separate
- **gRPC** for agent runners if you want a strong contract and streaming 

If you want a more “systems” implementation later:
- Rust/Go for the orchestrator can reduce runtime footguns, but only do this once the architecture stabilizes.

### Messaging systems

My opinionated recommendation for a local-first multi-process system:

- **Start with orchestrator-mediated messaging** (no broker). This is simpler and still robust if you design it as actor mailboxes + event store.
- Add a broker only when you need independent consumers or high fanout:
  - **NATS Core + optional JetStream**: core is at-most-once; JetStream adds persistence and stronger delivery semantics.   
  - **Redis Streams** if you want an append-only log with consumer group semantics and explicit pending/ack tracking.   

Be aware of durability details:
- JetStream’s docs explain acknowledged messages may not be `fsync`’d immediately under default settings, which matters for single-node “power-loss durable” guarantees. 

### Realtime UI transport

**SSE**  
Pros:
- simple, HTTP-native
- reconnection semantics and `Last-Event-ID` are standardized   
Cons:
- unidirectional (server→client)   

**WebSockets**  
Pros:
- true bidirectional “interactive session” transport   
Cons:
- classic `WebSocket` API has no backpressure mechanism; if messages arrive too fast, buffering can blow up memory or CPU.   

Opinionated take for your constraints:
- Keep **SSE for streaming telemetry and token output** (it’s hard to beat for simplicity).
- Use **HTTP POST** for commands initially.
- Upgrade to **WebSockets** only when you need a “live console” experience or bidirectional multiplexing.

### State management choices

- **Event sourcing** is the right “shape” for your system, but implement it intentionally: typed events, replay, projections.   
- **SQLite + WAL** is an excellent local-first store, with clear concurrency semantics and checkpointing behavior.   
  - But remember: one writer at a time.   

For local-first sync:
- CRDTs (Automerge/Yjs) are well-suited when you truly need multi-device mergeable state.   
- The broader local-first principle set is articulated by Ink & Switch.   

### Observability stack

Adopt OpenTelemetry early:
- It’s explicitly designed to correlate signals (traces/metrics/logs) via context propagation across process boundaries.   
This will pay off immediately when you add agent runners and tool servers.

## Blueprint

### High-level architecture diagram described in text

Think of the platform as five layers:

**User Interface Layer**
- Web UI (timeline + consoles)
- Streams updates via SSE (or WebSocket)

**API Layer**
- Local HTTP API server (commands, config, lock ops)
- Auth is “local machine trust” by default, but still gate destructive actions

**Orchestration Layer**
- Orchestrator daemon (actor-style mailboxes)
- Router/dispatcher (uses deterministic policy + Ollama as assistant classifier when needed) 

**Agent Execution Layer**
- Agent Runner processes (one per provider or per agent type)
- gRPC streams for token/event output 
- Tool server(s) (filesystem, git, shell) with explicit permissions

**Storage Layer**
- SQLite event store + projections (WAL mode) 
- Optional vector store:
  - sqlite-vec 
  - or Qdrant locally 

### Component breakdown

**Event Store (SQLite)**
- Receives all state transitions as immutable events (append-only rows).
- Projections build queryable state: tasks, locks, agent configs, run status.

**Orchestrator**
- Maintains in-memory mailboxes for each agent/run (actor-like).
- Writes events to DB in batches (to avoid “one token = one commit”).
- Emits UI updates from the same event stream (so UI is never a separate truth).

**Agent Runners**
- Stable RPC interface for each provider.
- Responsible for provider rate limits, retries, streaming normalization, and error codes.
- Can start as wrappers around existing CLIs; later migrate to native APIs.

**UI Streamer**
- Subscribes to new events and streams them to clients.
- Supports replay from an event ID (SSE `Last-Event-ID` style) to recover after reload. 

**Observability**
- OTel traces and logs carry correlation IDs from UI request → orchestrator command → agent run. 

### Data flow end-to-end

A typical run becomes:

1) User creates/starts a task in the web UI  
2) UI sends `POST /tasks/start` (or WebSocket command)  
3) API writes `TaskStarted` event → SQLite  
4) Orchestrator observes the new event and schedules the next action (policy router + optional LLM classification via Ollama)   
5) Orchestrator sends a structured request to an Agent Runner over gRPC  
6) Agent Runner streams token chunks and tool-call events back (structured)  
7) Orchestrator writes `AgentTokenChunk` / `ToolRequested` / `ToolResult` / `AgentFinished` events  
8) UI streamer sends these events via SSE; if the UI disconnects, it reconnects and resumes from last event ID   
9) Projections update task status and lock state synchronously or near-real-time

### Migration path from the current system

This is designed to be incremental and low-risk.

**Phase: formalize the transcript without changing behavior**
- Introduce event IDs, timestamps, event types, and JSON schema validation in the transcript.
- Add correlation IDs (task/run IDs) everywhere.
- This turns the transcript into “proto-event-sourcing.”

**Phase: dual-write into SQLite**
- Keep appending to the transcript for comfort/debugging.
- Also write every event into SQLite (append-only event table).
- Build basic projections (tasks, runs, agents, locks).

**Phase: remove polling**
- Instead of polling a file, the orchestrator reads “new events since last ID” from SQLite.
- UI also streams from SQLite event IDs (or from an in-process pub/sub fed by SQLite commits).

**Phase: wrap CLIs behind Agent Runner RPC**
- Create a single runner per provider that internally still uses the CLI.
- Make streaming structured.
- Add health checks and timeouts (gRPC makes this pattern natural). 

**Phase: replace tmux as primary mirror**
- Build the web timeline and per-run logs as the canonical debug surface.
- Keep tmux as an optional developer convenience, not as a system dependency.

**Phase: upgrade routing and coordination**
- Move from “LLM decides everything” to “policy router + LLM only for ambiguity.”
- Add actor-like mailboxes and supervision semantics (restart runners on failure; mark runs failed safely). Actor model supervision is a well-known pattern in actor systems. 

## Version two architecture in plain English

Here’s how “version two” works, end-to-end:

You run one local daemon that acts like mission control. Every meaningful thing that happens—task creation, agent prompt construction, model call start, token chunks, tool calls, failures—is written as a structured event into a local SQLite database. The system state (task list, locks, run status) is just a projection of that event history, so it’s always reconstructible by replay, like event sourcing intends.

Agents aren’t invoked as one-off CLI commands anymore. Instead, each provider has a small “agent runner” process that exposes a stable streaming RPC interface (gRPC). The orchestrator tells a runner what to do; the runner streams back tokens and structured events. gRPC is designed for efficient RPC and supports streaming semantics.

The web UI is not a separate source of truth—it just subscribes to the event stream and renders it. For streaming updates, SSE remains a simple choice: it’s a standardized server→client stream (`text/event-stream`) with reconnection and resume semantics.

Debugging stops being “watch panes in tmux and guess.” Instead, you can click a run and see a deterministic timeline and correlated logs/traces. OpenTelemetry provides the conceptual model for correlating signals across boundaries using context propagation.

Why it’s better: you keep the local-first, inspectable feel, but you stop outsourcing correctness to a text file and polling loop. You get structured recovery, stable interfaces, and real observability—without turning your local system into a complex distributed platform.

---

---

# Runtime Stability Sprint — Five Structural Bug Fixes
> Added: 2026-03-23 | Status: PENDING IMPLEMENTATION | Owner: CODEX (push/merge), CLAUDE (review), GEMINI (verify)

## Context

During live operation the system exhibited three user-visible failures:
1. Agents appeared “out of fuel” after only a few message exchanges
2. Codex stopped responding entirely after its first successful reply
3. The relay log became unreadable, burying all real error signals

Root-cause analysis identified five structural defects — not surface symptoms. Every fix below is a **permanent architectural correction**, not a patch or workaround.

---

## BUG-1 — Double Token Counting (CRITICAL)

### What is broken
`supervisor.py` calls `record_agent_usage()` **twice** for every single agent reply:

**Path A** — `handle_relay_event()`, `transcript` handler (`supervisor.py:1084-1089`):
```python
# triggered when append_reply() fires a transcript event
if speaker and speaker in agents and char_count > 0:
    estimated_tokens = max(1, char_count // 4)
    self.state.record_agent_usage(speaker, estimated_tokens)  # ← COUNT #1
```

**Path B** — `handle_relay_event()`, `agent_state` handler (`supervisor.py:1112-1117`):
```python
# triggered when route_to() fires agent_state(state=”ready”)
if “tokens_delta” in event:
    tokens = event[“tokens_delta”]
    if tokens > 0:
        self.state.record_agent_usage(event[“agent”], tokens)  # ← COUNT #2
```

Both fire for every reply. With `DEFAULT_USAGE_LIMIT = 50,000` tokens and 2× counting, the gauge exhausts after ~25,000 chars of combined agent output — roughly **3–5 long responses**.

Additionally, Path A counts **human messages** too (any speaker whose name matches an agent key — which does not happen, but SYSTEM messages do fire the transcript event), compounding the drain.

### Root cause
The `transcript` event’s `char_count` field was added for speaker-tracking bookkeeping, not fuel accounting. The `tokens_delta` field in `agent_state` is the intentional per-agent fuel accounting path. Both were wired to the same `record_agent_usage()` call without removing the redundant path.

### Permanent fix
**File:** `supervisor.py`

Remove the token accounting block from the `transcript` event handler entirely. Keep only the `tokens_delta` path in the `agent_state` handler.

```python
# supervisor.py — transcript handler
# REMOVE these lines (currently ~1084-1089):
#   speaker = event.get(“last_speaker”, “”).strip().upper()
#   char_count = event.get(“char_count”, 0)
#   if speaker and speaker in self.state.snapshot()[“agents”] and char_count > 0:
#       estimated_tokens = max(1, char_count // 4)
#       self.state.record_agent_usage(speaker, estimated_tokens)
```

The `agent_state` tokens_delta path (supervisor.py:1112-1117) is the correct single source of truth. It fires once per agent reply, tied to the specific agent, with the precise reply character count.

---

## BUG-2 — “No Fuel Left” = Any Error State (CRITICAL)

### What is broken
`web/app.js:6`:
```js
const stateLabels = {
  error: “no fuel left”,   // ← shown for ALL errors, not just fuel exhaustion
};
```

Every failure — session conflict, API timeout, missing binary, rate limit, wrong flags — displays as “no fuel left”. The actual error text from `last_error` is never shown in the agent card. Operators cannot distinguish a transient session conflict (recoverable in seconds) from a genuine resource exhaustion.

This label also makes the fuel metaphor load-bearing when it isn’t — the fuel limit is tracked but **never enforced** (no routing gate exists that blocks dispatch when fuel is low).

### Permanent fix
**File:** `web/app.js`

1. Change `stateLabels.error` from `”no fuel left”` to `”error”`.
2. In the agent card render function, surface `last_error` as a subtitle/tooltip beneath the state label so the actual failure reason is always visible.
3. Change `stateNotes.error` to accurately describe what error state means: a failed invocation, not capacity exhaustion.

```js
// web/app.js — stateLabels
const stateLabels = {
  starting: “spinning up”,
  auth: “tool online”,
  warming: “routing traffic”,
  ready: “ready”,
  error: “error”,   // was: “no fuel left”
};

const stateNotes = {
  // ...
  error: “The last routing cycle failed. Check the agent card for the specific error.”,
};
```

In the agent card renderer, add a `last_error` line below the state label when `state === “error”` and `last_error` is non-null.

---

## BUG-3 — Codex Always Fails After First Session (CRITICAL)

### What is broken
In `relay.py` and `config.json`, Codex has two invocation paths:

**First call (no session_id)** — uses `args`:
```json
[“exec”, “--skip-git-repo-check”, “--dangerously-bypass-approvals-and-sandbox”, “-C”, “{script_dir}”]
```
→ `codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -C /path prompt`
✓ Works.

**Every subsequent call (session_id exists)** — uses `invoke_resume_args`:
```json
[“exec”, “resume”, “{session_id}”]
```
→ `codex exec resume abc123`
✗ Fails: `Not inside a trusted directory and --skip-git-repo-check was not specified.`

**Confirmed in relay.log:**
```
[relay 2026-03-23 14:06:44] CODEX: stderr: Not inside a trusted directory and --skip-git-repo-check was not specified.
[relay 2026-03-23 14:06:44] CODEX: error: exit code 1
```

After the session_id is persisted to `sessions.json` on first success, **every subsequent Codex invocation fails**. This is why Codex appears silent after its first reply.

### Root cause
`invoke_resume_args` was written as a minimal resume command but the Codex CLI’s directory-trust validation runs on every invocation, including resume. The trust bypass flags must be present regardless of invocation path.

### Permanent fix
**Files:** `config.json` AND `relay.py` DEFAULT_CONFIG (both must stay in sync)

Add the missing trust flags to Codex’s `invoke_resume_args`:

```json
“invoke_resume_args”: [
  “exec”,
  “--skip-git-repo-check”,
  “--dangerously-bypass-approvals-and-sandbox”,
  “-C”,
  “{script_dir}”,
  “resume”,
  “{session_id}”
]
```

**Verification step before merging:** Run `codex exec --help` and `codex exec resume --help` to confirm flag ordering. Flags for the `exec` subcommand must precede the `resume` subcommand argument.

---

## BUG-4 — HTTP Server Pollutes Relay Log with SSE Disconnect Tracebacks

### What is broken
`supervisor.py` uses Python’s `ThreadingHTTPServer` which inherits `BaseHTTPRequestHandler`. Every SSE client disconnect — normal browser behavior (tab close, reconnect, refresh) — generates a full Python traceback:

```
ConnectionResetError: [Errno 54] Connection reset by peer
  File “.../socketserver.py”, line 697, in process_request_thread
  ...
```

The relay.log is currently **almost entirely these tracebacks**, making it impossible to find real errors. Every real routing event, agent error, and session conflict is buried under hundreds of lines of normal connection churn.

### Root cause
`BaseHTTPRequestHandler.log_error()` is called for all socket errors, including benign ones like client disconnects. Python’s HTTP server has no built-in filter for expected disconnects on SSE endpoints.

### Permanent fix
**File:** `supervisor.py`

Override `log_error` on the request handler class to suppress `ConnectionResetError` specifically, which is always normal for long-lived SSE connections:

```python
class QuietRequestHandler(BaseHTTPRequestHandler):
    def log_error(self, format_str, *args):
        # ConnectionResetError is normal for SSE clients disconnecting.
        # Suppress it to keep relay.log readable for real errors.
        if args and “Connection reset” in str(args[0]):
            return
        super().log_error(format_str, *args)
```

Replace `BaseHTTPRequestHandler` with `QuietRequestHandler` as the handler class for the `ReusableHTTPServer`. Do not suppress `BrokenPipeError` — that also occurs but can indicate real write failures; leave it visible.

---

## BUG-5 — Agents Receive Raw JSON as Context (Context Formatting)

### What is broken
`relay.py:1587, 1796, 1878` — context passed to every agent prompt:
```python
fresh = read_text(log_path)
context = fresh[-context_len:]  # last 6000 raw chars of clcodgemmix.txt
```

`clcodgemmix.txt` stores each message as a raw JSON object per line:
```json
{“id”: “abc123”, “sender”: “Farhan”, “seq”: 1774232533222, “type”: “message”, “body”: “hi”, “ts”: “2026-03-23T02:22:13Z”}
```

A 6000-char raw slice:
- Can cut through the middle of a JSON object (the last line may be truncated mid-field)
- Contains `id`, `seq`, `type` structural noise agents don’t need
- Makes agents parse structural noise instead of reading conversation prose
- Produces confused or non-responsive replies when the context is truncated mid-entry

### Permanent fix
**File:** `relay.py`

Add a `format_context(raw: str, max_chars: int) -> str` function:
1. Parse each line of the transcript as a JSON object
2. Emit each parsed entry as `[SPEAKER] body` — clean, readable prose
3. Trim from the **oldest** end so the slice always ends at a complete message boundary
4. Fall back gracefully to raw tail if JSON parsing fails (backward compat)

Use this function in all three places that currently pass raw context:
- `dispatch_drain_loop()` at `relay.py:1878`
- `batch_pending_tasks()` at `relay.py:1796`
- The direct-route path at `relay.py:1587`

```python
def format_context(raw: str, max_chars: int) -> str:
    “””Convert raw JSON-per-line transcript to readable [SPEAKER] body format.
    Slices on message boundaries so context is never cut mid-entry.”””
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            sender = msg.get(“sender”, “?”)
            body = str(msg.get(“body”, “”)).strip()
            if body:
                lines.append(f”[{sender}] {body}”)
        except json.JSONDecodeError:
            lines.append(line)  # pass-through for non-JSON legacy lines

    # Build from newest backwards, never exceeding max_chars
    result_parts: list[str] = []
    total = 0
    for entry in reversed(lines):
        cost = len(entry) + 1  # +1 for newline
        if total + cost > max_chars:
            break
        result_parts.append(entry)
        total += cost
    return “\n”.join(reversed(result_parts))
```

---

## Implementation Assignments

| Task | Bug | Owner | Reviewer | Status |
|------|-----|-------|----------|--------|
| T-3 | BUG-1: Remove double token counting | CODEX | CLAUDE | pending |
| T-4 | BUG-3: Fix Codex invoke_resume_args | CODEX | CLAUDE | pending |
| T-5 | BUG-2: Fix UI error labels + surface last_error | CODEX | GEMINI | pending |
| T-6 | BUG-4: Suppress SSE disconnect log noise | CODEX | CLAUDE | pending |
| T-7 | BUG-5: Fix context formatting | CODEX | GEMINI | pending |

**CODEX is the sole push/merge authority.** CLAUDE reviews backend Python changes. GEMINI reviews frontend JS changes. No agent merges work assigned to another agent.

## Execution Order

Execute in this order — each fix is independent but P1 tasks remove blocking noise first:

1. **T-4 first** (BUG-3 Codex resume args) — unblocks Codex itself from the error loop
2. **T-3** (BUG-1 double counting) — stops the fuel gauge from false-alarming
3. **T-6** (BUG-4 log noise) — cleans relay.log so verification is possible
4. **T-5** (BUG-2 UI labels) — accurate error display
5. **T-7** (BUG-5 context formatting) — improves agent comprehension across all future sessions

## Verification Checklist (per task)

- [ ] BUG-3: Run `python3 -c “import json,pathlib; d=json.loads(pathlib.Path(‘config.json’).read_text()); print(next(a[‘invoke_resume_args’] for a in d[‘agents’] if a[‘name’]==’CODEX’))”` — confirm trust flags present
- [ ] BUG-1: Send 5 test messages, check `state.json` fuel gauge doesn’t hit 0 prematurely
- [ ] BUG-4: Restart supervisor, disconnect/reconnect browser — confirm relay.log shows no ConnectionResetError tracebacks
- [ ] BUG-2: Trigger an intentional agent error, confirm UI shows “error” not “no fuel left”, and last_error text is visible
- [ ] BUG-5: Inspect prompt passed to agent in agent IO log — confirm `[SPEAKER] body` format, no raw JSON
