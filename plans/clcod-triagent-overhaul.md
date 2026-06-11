# Blueprint — clcod Tri-Agent Room: Speed Overhaul + Industry-Grade Redesign

**Objective:** Make the clcod tri-agent room feel instant and live (kill the "coal-mine train" lag) and rebuild the UI to industry-grade, executed by the **Bohor fleet** with meaningful per-lane assignment.

**Repo:** `/Users/moofasa/clcod` · branch `main` · remote `github.com/gUBII/clcod.git` · git+gh available → **branch/PR mode**.
**Commit rule (HARD):** `Farhanfeat(scope):` / `Farhanfix(scope):` prefixes, body only, **zero attribution trailers** (no Co-Authored-By / AI mention).

---

## Empirical baseline (measured live 2026-06-12, not assumed)

UAT probe `@all` broadcast, observed via Claude-in-Chrome + backend inspection:

| Finding | Evidence | Implication |
|---|---|---|
| UI transport is already **live SSE** | `/api/events` held open (200); `/api/state` fired once, not every 1.5s | **Do NOT rebuild transport.** Not the bottleneck. |
| Replies land as **whole blocks, no streaming** | `relay.py:1444` `await asyncio.wait_for(proc.communicate(), ...)`; Claude's 2-line reply appeared atomically | **Lever: token streaming.** Replace `communicate()` with incremental stdout read. |
| Every turn is a **cold subprocess** | `relay.py:1436` `create_subprocess_exec` per dispatch | **Lever: warm/persistent sessions.** 5–30s startup tax per message. |
| Fan-out is **already parallel** | 3 LIVE LANES (CLAUDE/CODEX/GEMINI) lit `TXX` simultaneously; Claude+Gemini both back ~13s | **Lever mostly DONE.** Only needs timeout tuning + straggler isolation. |
| **Codex hangs to 180s timeout** | Real PID 4713, 1:55 elapsed, no reply; `relay.log` rmcp `AuthRequired invalid_token` against `mcp.vercel.com` | **Lever: fix Codex.** Root cause = `codex_apps` MCP revoked-token handshake on every `codex exec` boot. |
| Gemini default-model fix | `config.json` + `preferences.json` → `selected_model:"default"`; verified `build_selection_args==[]`; replied live on default | **DONE this session.** |

**Conclusion:** the lag is the **dispatch layer**, not the UI transport. Order of impact: (1) Codex MCP hang, (2) token streaming for perceived speed, (3) warm sessions for cold-start tax, (4) parallel fan-out hardening (small).

---

## Dependency DAG

```
S1 instrument/baseline (researcher)
      │
      ├──────────────┬───────────────┬─────────────────┐
      ▼              ▼               ▼                 ▼
S2 fix Codex     S3 token        S6 redesign:      (S6 has no code dep on
   MCP hang         streaming        design system      backend; parallelizable)
   (coder)          (coder)          (frontend)
      │              │               │
      │              ▼               │
      │           S4 warm            │
      │              sessions        │
      │              (coder)         │
      │              │               │
      └──────┬───────┴───────┐       │
             ▼               ▼       ▼
        S5 fan-out tuning   S7 redesign: wire live data + token deltas
           (coder)             (frontend)  ← depends on S3 frames + S6 shell
                     │               │
                     └──────┬────────┘
                            ▼
                   S8 adversarial review (reviewer + security)
                            ▼
                   S9 E2E live UAT (e2e)
```

**Parallel waves:** {S2, S3, S6} after S1 · S4 after S3 · {S5, S7} after their deps · S8 gate · S9 final.

---

## Steps (cold-start briefs)

### S1 — Instrument & baseline  · lane `researcher` · model default · read-only
**Context:** Before changing dispatch, measure it. The room dispatches via `relay.py` (`call_agent` 1498, `_exec_agent` 1436, dispatch job loop 2234–2429) and pushes SSE via `supervisor.py` (`sse_broadcast` 440).
**Tasks:** (1) Add structured timing logs around `_exec_agent` (spawn→first-byte→exit) per agent. (2) Document the exact dispatch path: where the job loop awaits, whether agents are `gather`ed or sequential, where the 180s `agent["timeout"]` applies. (3) Capture a 3-run latency table per engine. **No behavior change.**
**Verify:** `plans/_baseline.md` written with per-agent spawn/first-byte/total ms. **Rollback:** revert log lines.

### S2 — Fix Codex 180s timeout  · lane `coder` · model strongest · **highest impact**
**Context:** `codex exec` blocks on the `codex_apps` MCP (`mcp.vercel.com`) whose OAuth token is revoked (`relay.log` rmcp `AuthRequired invalid_token`; same `token_revoked` 401 seen on interactive `codex` launch). Claude/Gemini have no such MCP and return in ~13s.
**Tasks:** (1) Reproduce: time `codex exec` standalone with and without the dead MCP. (2) Disable the revoked MCP for clcod dispatch — preferred: pass codex a clcod-scoped config/`--config` (or env) that omits `codex_apps`, OR `codex mcp` remove/disable the dead server. **Do not** touch unrelated user codex auth. (3) Add a relay-side guard: drop the codex `timeout` 180→75s and classify MCP-handshake failure distinctly in the lane state. (4) Confirm a Codex round-trip < 30s.
**Verify:** live `@codex` probe returns < 30s; `clcodgemmix.txt` gets a CODEX row; no rmcp auth error in `relay.log`. **Rollback:** restore `config.json` codex block + any codex config change.

### S3 — Token streaming into transcript  · lane `coder` · model strongest
**Context:** `_exec_agent` uses `proc.communicate()` (blocks until exit). Each CLI supports incremental output (claude `--output-format stream-json`, codex `exec --json`, gemini `-o stream-json` per prior UAT findings doc `.claude/PRPs/plans/p3-engine-uat-findings.md`).
**Tasks:** (1) Replace `communicate()` with `async for line in proc.stdout` reading the engine's stream-json; parse partial text. (2) Emit incremental SSE frames (`sse_broadcast("token", {agent, delta, seq})`) as chunks arrive; keep a final consolidated message event for persistence/replay. (3) Add an `app.js` handler that appends deltas to a live-typing bubble, finalized on the message event. **Backend-patterns:** treat each engine adapter as a stream translator (service-layer separation); never block the event loop.
**Verify:** live probe shows text appearing progressively, not atomically; `Last-Event-ID` replay still reconstructs full messages. **Rollback:** feature-flag `STREAM_TOKENS`, default off → falls back to `communicate()`.

### S4 — Warm / persistent sessions  · lane `coder` · model strongest · depends S3
**Context:** Cold `create_subprocess_exec` per turn. Config already has `invoke_resume_args` / `mirror_resume_args` / `preseed_session_id` — resume reuses *context* but still cold-spawns the *process*.
**Tasks:** (1) Ensure every turn resumes its session id (no fresh cold context) consistently across all 3 engines. (2) Where an engine supports a persistent/interactive process, keep a warm long-lived process per agent behind a small session pool with health/restart (backend-patterns: pooled resource + retry-with-backoff). (3) Fall back to cold-spawn on pool miss. Measure startup-tax delta vs S1 baseline.
**Verify:** per-turn spawn→first-byte drops measurably vs `_baseline.md`. **Rollback:** disable pool, revert to resume-only.

### S5 — Fan-out tuning & straggler isolation  · lane `coder` · model default · depends S2,S4
**Context:** Fan-out already parallel (verified). Remaining: a stuck engine shouldn't make the room feel dead.
**Tasks:** (1) Confirm job loop uses `asyncio.gather` (not sequential await) for multi-target; fix if serial. (2) Per-agent timeout overrides; render a stuck lane as "still working / timed out" without blocking others (already partially present). (3) Circuit-breaker tuning so a repeatedly-failing engine is visibly skipped, not silently retried.
**Verify:** broadcast with one slow engine: other two render immediately; slow one degrades gracefully. **Rollback:** revert dispatch loop changes.

### S6 — Redesign: design system & shell  · lane `frontend` (siphon `frontend-design`) · model strongest · parallel with backend
**Context:** Single-file UI: `web/index.html`, `web/app.js` (64KB), `web/styles.css` (51KB). Current "engine-room / DSP console" theme is distinctive but rough (uniform spacing, low hierarchy, raw gauges). Keep the identity, elevate the craft.
**Tasks:** (1) Establish CSS-token design system (color/space/type scale/duration/easing) per web coding-style rules; OKLCH palette. (2) Rework layout hierarchy: clear scale contrast, intentional rhythm, designed hover/focus/active states, compositor-friendly motion only. (3) Keep DOM hooks `app.js` depends on stable (coordinate via data attributes) so backend work doesn't collide. **No data-flow changes in this step.**
**Verify:** visual-regression screenshots at 1024/1440; both still render live SSE data; no `app.js` selector breakage. **Rollback:** `styles.css`/`index.html` are isolated; revert files.

### S7 — Redesign: wire live data + token deltas  · lane `frontend` · model strongest · depends S3,S6
**Tasks:** (1) Connect the redesigned shell to all SSE event types incl. new `token` deltas from S3 (live-typing bubbles, lane states, fuel/usage). (2) Polish transcript, LIVE/RECENT LANES, engine cards, task board as one coherent system. (3) Reduced-motion + keyboard-nav pass.
**Verify:** live broadcast renders streaming text in the new design; a11y checks pass. **Rollback:** revert `app.js` view layer.

### S8 — Adversarial review  · lanes `reviewer` + `security` · model strongest · gate
**Tasks (reviewer):** correctness, no regression vs `_baseline.md`, SSE replay integrity, no event-loop blocking, attribution-trailer check. **Tasks (security):** the `-y`/`--dangerously-bypass-approvals-and-sandbox` posture, SSE auth (`_authorized`), no secret/token leak in logs or transcript, the codex MCP change doesn't broaden surface.
**Verify:** zero CRITICAL/HIGH open. **Rollback:** block merge; bounce findings to owning step.

### S9 — E2E live UAT  · lane `e2e` · model default · final
**Tasks:** Live room: broadcast → all 3 reply, Codex < 30s, text streams, redesigned UI renders; capture before/after latency table + screenshots.
**Verify:** Codex replies in data layer < 30s; streaming visible; design intact. **Rollback:** n/a (verification only).

---

## Lane map (meaningful, not one-bucket)

| Lane | Steps | Why |
|---|---|---|
| `researcher` | S1 | read-only measurement, no mutation |
| `coder` | S2,S3,S4,S5 | backend dispatch engineering |
| `frontend` (siphon `frontend-design`) | S6,S7 | design system + view layer |
| `reviewer` | S8 | correctness/regression gate |
| `security` | S8 | bypass-flag + SSE-auth + secret review |
| `e2e` | S9 | live behavioral verification |
| `orchestrator` | — | coordinates the DAG / merge order |

## Bohor dispatch shape
Project `clcod-triagent-overhaul` → one mission per step, `depends_on` per DAG, `auto_dispatch=1`, worktree-isolated. Wave 1 {S1}, Wave 2 {S2,S3,S6} after S1, then S4, then {S5,S7}, gate S8, final S9. Frontend lane is project-scoped (siphoned from the `frontend-design` ECC skill) since Bohor's built-ins lack a design lane.

## Invariants (checked every step)
- Room stays bootable (`healthcheck.sh` green) after each merge.
- SSE replay (`Last-Event-ID`) always reconstructs full transcript.
- No attribution trailers in any commit.
- No regression in Claude/Gemini latency vs `_baseline.md`.

---

## Review fixes (applied 2026-06-12 — adversarial gate: GO-WITH-FIXES)

These override the step briefs above where they conflict. Mandatory before fleet dispatch.

### C1 (CRITICAL) — S3 must enumerate the full 3-hop event chain
`supervisor.py:1125` `handle_relay_event` is an explicit `if event["type"]==...: return` chain with **NO default branch** → an unknown `token` event is silently dropped. S3 acceptance criteria now require ALL THREE hops, each independently verified:
1. `relay.py` emits the `token` frame,
2. `supervisor.handle_relay_event` gains a `token` branch that calls `sse_broadcast`,
3. `app.js` onmessage switch gains a `case "token"` renderer.
"Works in isolation" is not done; the hop-2 branch is a hard acceptance gate.

### C2 (CRITICAL) — token deltas must use a NON-persisting emit path
`relay.py:1201` `emit_event` persists every event via `event_store.append_event` (`event_store.py:267`), and `Last-Event-ID` replay (`supervisor.py:1632`) replays them. Streaming per-token through `emit_event` corrupts replay. S3 must add a broadcast-only path (no `append_event`) for `token` frames; **only the final consolidated `transcript` message is persisted**. Replay reconstructs from that final message alone. Add a test: stream a long reply, reconnect with `Last-Event-ID`, assert the transcript equals the consolidated message (no partial deltas).

### H1 (HIGH) — split S3 per engine; it is a parser + config rewrite, not a one-liner
Current parsers consume plain text, not stream-json (`parse_claude:1018`, `parse_codex:965`, `parse_gemini:1013`), and no engine requests structured output (`config.json:7,52,93`). S3 splits into:
- **S3a** — Claude `--output-format stream-json` vertical slice proving the full relay→supervisor→app.js pipe (incl. C1/C2 + `extract_session_id` under the new format).
- **S3b** — Codex `exec --json`. **S3c** — Gemini `-o stream-json`.
Each keeps the old parser for the `STREAM_TOKENS`-off fallback. Per-engine session-id extraction is a named risk in each.

### H4 (HIGH) — S2 is repo-local-only + HUMAN CHECKPOINT
S2 must NOT run any global `codex mcp remove` / auth mutation (un-rollbackable, outside the worktree). Allowed fix surface: a **clcod-scoped codex config file checked into the repo** + a `--config`/env arg added to the CODEX block in `config.json` so `codex exec` boots without `codex_apps`. Mark S2 as a human-approval checkpoint before merge — it is the only step with machine-global side effects.

### M3 (MEDIUM) — tests are acceptance criteria, not a separate phase
S3a/b/c: streaming parser unit tests + the C2 replay-reconstruction test. S4: startup-tax measurement test. S5: circuit-breaker + fan-out unit tests. No backend step is "done" without its tests (project rule: 80%).

## Fleet-dispatch guardrails (H2 + H3)
Worktree isolation makes collisions LOOK parallel-safe but defers them to merge. Therefore:
- **Single-writer files per wave.** `relay.py` dispatch region (S3/S4/S5) and `web/app.js` (S3/S6/S7) are each owned by ONE in-flight mission at a time. Pre-work split recommended: extract the `app.js` SSE-event switch into its own module so S3 (event handling) and S6/S7 (render) never touch the same file.
- **Merge-to-main between dependent waves.** `depends_on` terminal-state is necessary but NOT sufficient — a dependent step's worktree must branch from a base that already contains its predecessor's merged commits. The orchestrator merges wave N to `main` before wave N+1 is dispatched. Backend steps are gated serial on `relay.py`; only truly file-disjoint steps run concurrently.
- **Revised concurrency:** Wave 1 {S1}. Wave 2 {S2 (checkpoint), S6} — disjoint files (codex config vs styles.css). Then S3a→S3b→S3c serial on relay.py/app.js. Then S4, then S5 (all serial on relay.py). S7 after S3*+S6. Gate S8. Final S9. Frontend (S6) is the only safe long-running parallel to the backend chain, and only until S7 needs app.js.

### Out-of-scope guards for autonomous agents
- Gemini gRPC branch (`relay.py:1472` `call_gemini_grpc`) is dead in this config — do not "consolidate" it.
- `CircuitBreaker.is_open` (`relay.py:1407`) mutates failure count on half-open intentionally — do not "fix."
- S1 must confirm the local ollama dispatcher (`config.json:145-155`) state before recording baseline latency (a dead router changes dispatch path).
