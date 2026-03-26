# Current Plan — Critical Bug Fixes & Stability Improvements

> Historical planning note only.
> Do not treat this file as the live runtime spec.
> Phase 2.2 task state normalization has since shipped.
> Current truth lives in `docs/architecture.md` and the code/tests.

**Created:** 2026-03-24
**Updated:** 2026-03-24
**Status:** ALL ISSUES COMPLETE (10/10)

---

## Issue Summary

| # | Issue | Location | Severity | Status |
|---|-------|----------|----------|--------|
| 1 | Lock release swallows errors | `relay.py:703-708` | Critical | **DONE** |
| 2 | Advisory flock may corrupt transcript | `relay.py:732-736` | Critical | **DONE** |
| 3 | Dispatcher silent fallback | `dispatcher.py:120` | High | **DONE** |
| 4 | SSE queue memory leak | `supervisor.py:382-408` | High | **DONE** |
| 5 | State duplication across 3 stores | `state.json`, `tasks.json`, `events.db` | Medium | **DONE** |
| 6 | No circuit breaker for agents | `relay.py` agent calls | Medium | **DONE** |
| 7 | Transcript context loading is O(N) | `relay.py:1589,1798,1880` | Medium | **DONE** |
| 8 | Password auth not hashed | `supervisor.py` / `config.json` | Low | **DONE** |
| 9 | `agent.py` is dead code | `agent.py` | Low | **DONE** |
| 10 | `/api/dispatcher/health` undocumented | `supervisor.py` | Low | **DONE** (was already implemented) |

---

## Phase 1: Quick Wins — COMPLETE

All items merged and tested (63/63 tests pass).

### 1.1 Dispatcher Fallback Enhancement (Issue #3)

**File:** `dispatcher.py`
**What changed:**
- Added `_TRANSIENT_ERRORS` tuple: `(urllib.error.URLError, TimeoutError, ConnectionError, OSError)`
- Added `_log_dispatcher()` helper that prints to stderr with `[dispatcher]` prefix
- `classify_message()` now retries transient errors with exponential backoff:
  - Configurable via `config["router_retries"]` (default: 2)
  - Delay: `0.5 * (2 ** attempt)` seconds between retries
  - JSON parse errors (`JSONDecodeError`) fail immediately — no retry
- Fallback responses include `"fallback": True` flag so callers can detect degraded routing
- All failures logged with error type and attempt count

**Config:** `dispatcher.router_retries` (int, default 2)

**Tests added:**
- `test_classify_message_retries_on_transient_error` — verifies retry on URLError, succeeds on 2nd attempt
- `test_classify_message_fallback_after_exhausted_retries` — verifies fallback flag after all retries exhausted
- `test_classify_message_no_retry_on_json_error` — verifies no retry on parse error (1 call only)

### 1.2 SSE Subscriber Cap & Cleanup (Issue #4)

**Files:** `supervisor.py`, `relay.py`
**What changed:**
- `sse_subscribe()` returns `None` when `len(_sse_clients) >= max_sse_subscribers`
- HTTP handler returns `503 Service Unavailable` when cap reached
- Moved `sse_subscribe()` call before HTTP response headers to avoid partial response on rejection
- `sse_broadcast()` logs dropped full queues to stderr with remaining client count
- Added `sse_client_count()` method for observability
- Added `ui.max_sse_subscribers` to `load_config` return structure (default: 32)
- `RuntimeSupervisor.__init__` reads from `config["ui"]["max_sse_subscribers"]`

**Config:** `ui.max_sse_subscribers` (int, default 32)

**Tests added:**
- `test_subscribe_respects_max_limit` — cap at 2, third subscribe returns None
- `test_unsubscribe_frees_slot` — unsubscribe + resubscribe succeeds
- `test_broadcast_drops_full_queues` — full queue is evicted, healthy queue receives event

### 1.3 Lock Release Error Handling (Issue #1)

**File:** `relay.py`
**What changed:**
- `release_lock()` now only catches `FileNotFoundError`
- All other `OSError` subtypes (`PermissionError`, etc.) propagate to caller
- Before: bare `except OSError: return` swallowed everything silently

**Tests added:**
- `test_release_lock_ignores_missing_file` — no exception on nonexistent lock
- `test_release_lock_propagates_unexpected_oserror` — `PermissionError` raises

---

## Phase 2: Core Fixes — PARTIAL (2.1 + 2.3 complete, 2.2 pending)

### 2.1 Transcript Concurrent Writes (Issue #2) — COMPLETE

**File:** `relay.py:712-736` — `persist_transcript_message()`
**Current code:**
```python
with log_path.open("a", encoding="utf-8") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    handle.write(entry)
    handle.flush()
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
```

**Problem:**
- `fcntl.flock()` is advisory — another process that doesn't call `flock()` can write freely
- Blocking `LOCK_EX` can deadlock if a process dies while holding the lock
- No retry or timeout on lock acquisition

**Proposed fix:**
1. Use non-blocking `LOCK_NB` with a retry loop and timeout
2. Wrap in `try/finally` to guarantee unlock even on write failure
3. Add `BlockingIOError` handling with configurable retry count
4. Consider using `os.open()` with `O_APPEND | O_CREAT` for atomic appends (kernel-level guarantee on Linux/macOS for writes < PIPE_BUF)

**Implementation plan:**
```python
import errno

def persist_transcript_message(...) -> dict[str, Any]:
    # ... message construction unchanged ...

    entry = json.dumps(message) + "\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    max_lock_attempts = 10
    lock_backoff = 0.05  # 50ms

    with log_path.open("a", encoding="utf-8") as handle:
        for attempt in range(max_lock_attempts):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == max_lock_attempts - 1:
                    raise
                time.sleep(lock_backoff * (2 ** attempt))
        try:
            handle.write(entry)
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
```

**Risk:** Medium — changes write path for all transcript messages
**Mitigation:** Keep advisory lock (compatible with existing code); retry is additive behavior

### 2.2 State Normalization (Issue #5)

**Current state:** Three separate stores
- `state.json` — in-memory `StateStore` (supervisor.py:281), file-backed, holds agent states, transcript metadata, task counts
- `tasks.json` — file-based task list (relay.py `load_tasks`/`save_tasks`), read by both supervisor and relay
- `events.db` — SQLite `EventStore` (event_store.py:109), append-only event log with dispatch queue

**Problem:** Inconsistencies possible when:
- Task status updates in `tasks.json` but `state.json` task counts lag behind
- Events in `events.db` describe state transitions that `state.json` doesn't reflect
- Crash between writing one store and another

**Proposed fix (incremental):**
1. Make `events.db` (EventStore) the **single source of truth** for all state transitions
2. `state.json` becomes a **materialized view** — rebuilt from events on startup, updated in-memory during runtime
3. `tasks.json` writes become **dual-write**: write to EventStore first, then update tasks.json for backward compatibility
4. Add a `rebuild_state_from_events()` method to `RuntimeSupervisor`

**Implementation plan:**
1. Add event types: `task_created`, `task_updated`, `task_completed` to EventStore
2. Add `StateStore.rebuild(event_store: EventStore)` method
3. On supervisor startup: rebuild state from events, reconcile with tasks.json
4. Keep tasks.json writes for now (human-readable backup)
5. Long-term: remove tasks.json, derive from events only

**Risk:** High — touches core data flow
**Mitigation:** Keep dual-write during transition; add reconciliation check on startup

### 2.3 Circuit Breaker for Agents (Issue #6) — COMPLETE

**Current code:** `relay.py` — `route_to()` and `call_agent()` invoke agent CLIs with no failure tracking. If an agent times out or crashes, the next invocation still attempts it.

**Proposed fix:**
Add a simple per-agent circuit breaker:

```python
class AgentCircuitBreaker:
    def __init__(self, failure_threshold: int = 3, reset_timeout: float = 300.0):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._failures: dict[str, int] = {}
        self._last_failure: dict[str, float] = {}

    def record_failure(self, agent_name: str) -> None:
        self._failures[agent_name] = self._failures.get(agent_name, 0) + 1
        self._last_failure[agent_name] = time.time()

    def record_success(self, agent_name: str) -> None:
        self._failures.pop(agent_name, None)
        self._last_failure.pop(agent_name, None)

    def is_open(self, agent_name: str) -> bool:
        failures = self._failures.get(agent_name, 0)
        if failures < self.failure_threshold:
            return False
        last = self._last_failure.get(agent_name, 0)
        if time.time() - last > self.reset_timeout:
            # Half-open: allow one attempt
            self._failures[agent_name] = self.failure_threshold - 1
            return False
        return True
```

**Integration points:**
- `relay.py:route_to()` — check `circuit_breaker.is_open(agent_name)` before dispatch
- On `call_agent()` success → `record_success()`
- On timeout/error → `record_failure()`
- Emit `agent_state` event with `"state": "circuit_open"` for UI visibility
- Configurable via `locks.circuit_breaker_threshold` and `locks.circuit_breaker_reset`

**Risk:** Medium — affects agent dispatch path
**Mitigation:** Default threshold of 3 failures is conservative; half-open allows recovery

---

## Phase 3: Optimization & Cleanup — PARTIAL (3.1 complete, 3.2–3.4 pending)

### 3.1 Transcript Context Streaming (Issue #7) — COMPLETE

**Current code:** Three locations read entire transcript into memory:
- `relay.py:1589` — `context = fresh[-context_len:]`
- `relay.py:1798` — `context = fresh[-context_len:]`
- `relay.py:1880` — `context = fresh[-context_len:]`

Where `fresh = read_text(log_path)` reads the entire file.

**Problem:** O(N) memory per message as transcript grows. A 10MB transcript allocates 10MB per poll cycle.

**Proposed fix:**
```python
def read_tail(path: Path, max_bytes: int) -> str:
    """Read the last max_bytes of a file efficiently."""
    try:
        size = path.stat().st_size
        if size <= max_bytes:
            return path.read_text(encoding="utf-8")
        with path.open("r", encoding="utf-8") as f:
            f.seek(max(0, size - max_bytes))
            # Skip partial line
            f.readline()
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""
```

Replace `read_text(log_path)[-context_len:]` with `read_tail(log_path, context_len * 4)` (4x for multi-byte chars).

**Risk:** Low — read-only optimization
**Note:** `context_len` is in characters (default 6000). Need to estimate byte ratio.

### 3.2 Password Hashing (Issue #8)

**Current code:** `config.json` stores password in plaintext. `supervisor.py` compares directly.

**Proposed fix:**
- Hash password at startup using `hashlib.scrypt` or `secrets.compare_digest`
- Store hash in runtime state, not plaintext
- Accept plaintext in config for convenience, hash on load

**Risk:** Low — isolated to auth path

### 3.3 Dead Code Removal (Issue #9)

**File:** `agent.py`
- No imports found in `supervisor.py`, `relay.py`, or any other active code
- README references it as legacy
- **Action:** Remove `agent.py` after confirming no runtime references

**Risk:** Low — confirmed unused

### 3.4 Missing Health Endpoint (Issue #10)

**README** documents `/api/dispatcher/health` but it doesn't exist in `supervisor.py`.

**Proposed fix:** Add endpoint to HTTP handler:
```python
if parsed.path == "/api/dispatcher/health":
    health = await dispatcher_mod.health_check(...)
    return self._json(health)
```

**Risk:** Low — additive only

---

## Architecture Recommendations (Future)

These are non-urgent improvements identified during the audit.

### A. Event Sourcing Convergence
- Events are already stored in `events.db`
- Add projections to derive current state from event replay
- Enables time-travel debugging and crash recovery
- **Prerequisite:** Phase 2.2 state normalization

### B. Transcript Compaction
- Shard transcript by date (e.g., `2026-03-24.log`)
- Link shards in `transcript.index`
- Auto-compact old shards via summarization
- Reduces context loading time and memory

### C. Backpressure & Rate Limiting
- Rate limit incoming messages at socket handler
- Queue overflow handling with graceful degradation
- Per-agent backpressure based on circuit breaker state

### D. Agent Connection Pooling
- Maintain persistent connections to Ollama
- Reuse for multiple chat completions
- Reduces latency per dispatcher call

### E. Metrics & Observability
- Message throughput (msg/sec)
- Agent latency percentiles (p50, p95, p99)
- Token usage per window
- Dispatcher fallback rate
- SSE client count over time

---

## File Reference

### Active Implementation Files
| File | Purpose | Lines | Phase |
|------|---------|-------|-------|
| `supervisor.py` | HTTP server, tmux manager, state store | ~1870 | 1,2 |
| `relay.py` | Transcript watcher, agent router, locks | ~2000 | 1,2,3 |
| `event_store.py` | SQLite event store, dispatch queue | ~200 | 2 |
| `dispatcher.py` | Ollama routing layer | ~200 | 1 |
| `config.json` | Runtime configuration | — | 1,2 |

### Test Files
| File | Tests | Phase |
|------|-------|-------|
| `tests/test_dispatcher.py` | 5 tests (3 new) | 1 |
| `tests/test_relay.py` | 20 tests (2 new) | 1,2 |
| `tests/test_supervisor.py` | 22 tests (3 new) | 1,2 |
| `tests/test_queue_and_sse.py` | 16 tests | — |

### Confirmed Dead Code
| File | Reason |
|------|--------|
| `agent.py` | No imports found in any active module |

---

## Execution Order for Next Session

~~1. Phase 2.1 — DONE~~
~~2. Phase 2.3 — DONE~~
~~3. Phase 3.1 — DONE~~

1. **Phase 2.2** — State normalization (supervisor.py + event_store.py) — HIGH RISK, read event_store.py first
2. **Phase 3.2** — Password hashing (supervisor.py) — isolated, low risk
3. **Phase 3.3** — Remove agent.py dead code
4. **Phase 3.4** — Add `/api/dispatcher/health` endpoint

Items 2–4 are independent and can be done in any order or parallel.
Item 1 (state normalization) should be done alone — highest blast radius.

---

## Risk Matrix

| Phase | Item | Risk | Blast Radius | Rollback |
|-------|------|------|-------------|----------|
| 2.1 | Transcript flock | Medium | All transcript writes | ~~done~~ |
| 2.2 | State normalization | High | Startup + all state reads | Keep dual-write, revert to tasks.json primary |
| 2.3 | Circuit breaker | Medium | Agent dispatch path | ~~done~~ |
| 3.1 | Context streaming | Low | Read-only optimization | ~~done~~ |
| 3.2 | Password hashing | Low | Auth path only | Revert to plaintext compare |
| 3.3 | Dead code removal | Low | None (unused file) | Restore from git |
| 3.4 | Health endpoint | Low | New endpoint only | Remove route |
