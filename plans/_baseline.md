# S1 — Dispatch & Write-Latency Baseline

**Measured:** 2026-06-12 · host: darwin (Apple Silicon) · repo `/Users/moofasa/clcod`
**Method:** cold-subprocess timed harness that mirrors `relay._exec_agent` exactly
(`asyncio.create_subprocess_exec` → `asyncio.wait_for(proc.communicate(), timeout=…)`),
using the same sessionless command shape the room builds (`relay._build_sessionless_command`,
base args + selection args from `config.json`). Prompt: *"Reply with exactly the single word
PONG and nothing else."* — a minimal-output probe so the number is dominated by **process
startup + model first-token latency**, not generation length. Each engine got 3 cold runs.

> Read-mostly step. The only code change is structured timing logging inside
> `_exec_agent` (every added line carries a `# S1-INSTRUMENT` marker). No control flow,
> output, or behavior changed.

---

## Per-engine timing (3 cold runs each)

Under the current dispatch model `_exec_agent` blocks on `proc.communicate()`, which drains
the pipes only after the child closes them. **There is no observable "first byte" before exit** —
spawn→first-byte is identical to spawn→exit. That equality *is* the headline baseline fact:
replies land as whole blocks, never streamed (the lever S3 targets).

| Engine | Run 1 (ms) | Run 2 (ms) | Run 3 (ms) | Mean (ms) | spawn→first-byte | rc | Reply |
|--------|-----------:|-----------:|-----------:|----------:|------------------|----|-------|
| CLAUDE | 6175 | 7009 | 7463 | **6882** | == exit (blocking) | 0 | `PONG` |
| GEMINI | 11870 | 10118 | 10339 | **10776** | == exit (blocking) | 0 | `PONG` |
| CODEX  | 3529 | 4952 | 3394 | **3958** | == exit (blocking) | 0 | `PONG` |

Cross-check (codex via `/usr/bin/time` standalone, no Python harness overhead):
real **3.02 / 3.42 / 3.51 s** — consistent with the harness numbers.

### Single biggest latency contributor, per engine

- **CLAUDE (~6.9 s):** cold CLI + Node/agent boot + model first-token. No streaming, so the
  full ~7 s is dead air in the UI. Dominant cost = **cold-process startup tax** (S4 warm
  sessions) compounded by **no token streaming** (S3 perceived-speed lever).
- **GEMINI (~10.8 s):** slowest engine. Same cold-boot tax plus a heavier CLI/model
  cold-start; first-token latency on the Gemini CLI is the dominant term. Streaming (S3) and
  warm sessions (S4) are the levers; its `timeout` is already generous at 300 s.
- **CODEX (~4.0 s):** **fastest** engine — see the major finding below. Dominant cost is the
  same cold-subprocess + first-token tax, with no MCP penalty observed.

---

## ⚠️ Major finding — the Codex 180 s hang did NOT reproduce

The plan's premise (`plans/clcod-triagent-overhaul.md` lines 20, 66–69) is that `codex exec`
blocks ~180 s on a revoked `codex_apps` MCP OAuth handshake (`mcp.vercel.com`,
`AuthRequired invalid_token`). **That no longer reproduces in this environment.**

Evidence:
- `codex exec …` returns a clean `PONG` (rc 0) in **3.0–6.1 s across 6 runs** — Codex is the
  *fastest* of the three engines, not a hang.
- `codex mcp list` → **"No MCP servers configured yet."** The `codex_apps` MCP that caused
  the revoked-token handshake is **gone**, so there is no MCP boot to stall on.

**Implication for the DAG:** S2 ("Fix Codex 180 s timeout") appears **already resolved /
moot** for standalone `codex exec`. Recommended before S2 is dispatched: confirm whether the
hang still occurs via the room's *resume* path (`mirror_resume_args` / `invoke_resume_args`
in `config.json`), which differs from the sessionless `exec` measured here. If the resume
path is also clean, S2 collapses to the relay-side guard only (drop codex `timeout` 180→75 s,
distinct MCP-handshake classification) and the human-checkpoint MCP-config surgery (H4) can be
skipped. The codex `timeout: 180` in `config.json` is currently ~45× the observed round-trip.

---

## Ollama dispatcher state

| Check | Result |
|-------|--------|
| `curl -s http://localhost:11434/api/tags` | **DOWN** — connection refused (curl exit 7) |
| `config.json` `dispatcher.enabled` | `true` (router `qwen3.5:latest`, host `http://localhost:11434`) |

The local ollama router is **not running**. With the dispatcher enabled but unreachable,
the configured `fallback_action: "route"` governs the path — routing decisions fall back
rather than being LLM-classified. This does **not** add latency to the agent round-trips
measured above (those bypass the router), but a *dead* router vs a *live* router is a
different dispatch path, so the baseline above reflects the **router-down** condition.
If S2–S5 measurements are taken with ollama *up*, re-baseline first to avoid comparing
across two different routing paths.

---

## Annotated dispatch path (file:line anchors)

Hot path for one operator message → agent replies → UI, top to bottom:

### relay.py — dispatch engine
- **`relay_log` — `relay.py:194`** — all relay logging; `S1-TIMING` lines emit here.
- **`dispatch_drain_loop` — `relay.py:2325`** — single consumer of the dispatch queue.
  - `claim_next_dispatch` — `relay.py:2335` — **serial across queued jobs**: one job claimed
    at a time, `await asyncio.sleep(0.3)` poll when idle. Queued messages are processed
    one-after-another, *not* concurrently.
  - Prompt/route build — `relay.py:2360–2391`.
  - **Fan-out — `relay.py:2404` `await asyncio.gather(route_to(...) for agent in target_agents)`**
    — agents in a *single* job run **in parallel** (CLAUDE/CODEX/GEMINI dispatched together).
    The job-loop barrier is the `gather`: a job is "done" only when its slowest agent returns
    (`complete_dispatch` — `relay.py:2422`), so one slow engine stalls that job's completion
    event but not the other agents' replies.
- **`route_to` — `relay.py:1578`** — per-agent wrapper. Emits `route_state`/`agent_state`
  (`warming`) before dispatch (`relay.py:1597–1613`); checks `circuit_breaker.is_open`
  (`relay.py:1617`, OUT OF SCOPE — half-open mutation is intentional); calls `call_agent`
  (`relay.py:1645`); on reply persists via `append_reply` (`relay.py:1647`).
- **`call_agent` — `relay.py:1498`** — builds the command under `session_lock`
  (`build_agent_command` — `relay.py:1327`, sessionless variant `_build_sessionless_command`
  — `relay.py:1318`), invokes `_exec_agent` (`relay.py:1518`), then session-in-use retry
  ladder (`relay.py:1525–1551`). The Gemini gRPC branch (`relay.py:1505` → `call_gemini_grpc`
  `relay.py:1472`) is **dead in this config** (no `grpc_target`) — OUT OF SCOPE.
- **`_exec_agent` — `relay.py:1430`** ← **THE LATENCY SITE** —
  `asyncio.create_subprocess_exec` (`relay.py:1436`, a **cold subprocess every turn**) then
  `asyncio.wait_for(proc.communicate(), timeout=agent["timeout"])` (`relay.py:1444`, **blocks
  until the child exits — no streaming**). `# S1-INSTRUMENT` logs wrap spawn / timeout / exit
  here.

### supervisor.py — HTTP + SSE bridge
- **`handle_relay_event` — `supervisor.py:1125`** — relay→SSE bridge. An **explicit
  `if event["type"] == …: return` chain with NO default branch** — `relay_state`
  (`:1127`), `transcript` (`:1139`), `agent_state` (`:1167`), … An unrecognized event type
  (e.g. a future `token` frame) is **silently dropped**. Each branch calls `sse_broadcast`.
- **`sse_broadcast` — `supervisor.py:440`** — fan-out to all connected SSE client queues via
  `q.put_nowait`; full queues are evicted (`:447–453`). This is the live transport to the web
  UI (already SSE; not the bottleneck).

### Path summary
```
operator msg → enqueue_dispatch
   → dispatch_drain_loop (relay.py:2325)         [serial across jobs]
       → asyncio.gather over agents (relay.py:2404)   [parallel within a job]
           → route_to (relay.py:1578)
               → call_agent (relay.py:1498)
                   → _exec_agent (relay.py:1430)   ← cold subprocess + blocking communicate()
       → append_reply → emit_event
   → supervisor.handle_relay_event (supervisor.py:1125)  [explicit chain, no default]
       → sse_broadcast (supervisor.py:440) → web UI
```

---

## Baseline conclusions (ordered by impact)

1. **No token streaming** — every reply is atomic; the entire 4–11 s is UI dead air. Highest
   *perceived*-speed lever (S3).
2. **Cold subprocess per turn** — `create_subprocess_exec` on every dispatch; the startup tax
   is the bulk of the floor on Claude/Gemini (S4 warm sessions).
3. **Gemini is the real slow engine (~10.8 s)**, not Codex. Re-prioritize accordingly.
4. **Codex hang premise is stale** — Codex is fastest (~4 s), MCP list empty. S2 likely moot
   for `exec`; verify the resume path before spending the highest-impact slot on it.
5. **Ollama router is down** — baseline taken in router-down condition; re-baseline if it is
   brought up before later steps.

### Instrumentation revert
The added logging lives only in `relay.py` `_exec_agent`, every line tagged `# S1-INSTRUMENT`:
```bash
grep -n "# S1-INSTRUMENT" relay.py     # list the added lines
```
Remove those lines (and restore the original 18-line `_exec_agent` body) to fully revert.
The live `clcod-4173` supervisor was running the pre-instrumentation `relay.py` during
measurement; a restart is required for `S1-TIMING` lines to appear in `relay.log`.
