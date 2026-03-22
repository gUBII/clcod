# Current Issues — UAT 2026-03-22

Acknowledged by: Claude (agent)
Reported by: Farhan

---

## 1. Project Dropdown Hidden Behind Engine Control

**Severity:** Blocker (unusable)
**Location:** `web/styles.css:129`, `web/index.html:21`

The `.project-dropdown` has `z-index: 100` but sits inside `.chrome__project` (which uses `position: relative`). When the dropdown opens, it falls behind the `.engine-strip` section and other workspace content below the chrome bar. The main content area creates its own stacking context that paints over the dropdown.

**Root cause:** The dropdown's z-index only competes within its parent stacking context (`.chrome`). The `.engine-strip` and `.workspace` sections are later in DOM order and overlap the dropdown visually.

**Fix needed:** Either raise the `.chrome` bar's z-index above all main content, or pull the dropdown out of the `.chrome__project` container so it participates in the root stacking context.

---

## 2. Redundant Gauge Next to "Live Workspace"

**Severity:** Medium (UI clutter)
**Location:** `web/index.html:96`, `web/app.js:718-730`

The `#workspaceTachs` div renders small tachometer gauges (`htach` elements) next to the "Live Workspace" eyebrow. These duplicate the gauges already shown inside the Engine Control strip (`#statusGrid`). The user sees the same per-agent fuel/pressure info twice.

**What it should be:** A **workspace handler** — the Sync Repo button is already present (`#syncRepoBtn`, line 102 in index.html), so the half-done workspace handler scaffold exists. The tach gauges should be replaced with workspace-level controls (project state, sync status, branch indicator, etc.), not a second set of engine gauges.

---

## 3. Compact Context Button — Wrong Behavior

**Severity:** High (functional mismatch)
**Location:** `web/app.js:373-391`

Currently the "Compact Context" button just calls `POST /api/compact` and shows a transient "Compacted" label. The intended behavior is:

- **Archive** the current chat transcript
- **Generate a verbose summary** of the archived conversation
- **Insert that summary** into the transcript as a condensed record

Right now it does none of this — it fires a backend call with no visible archival or summary injection into the transcript panel.

---

## 4. DSP Routing TXX / RXX Lights Not Glowing Per Engine

**Severity:** High (feature non-functional)
**Location:** `web/app.js:944-950` (fireSignalLight), `web/styles.css:2065-2093` (sig-light styles), `web/app.js:751-755` (sig-light HTML in control cards)

The TX/RX signal LEDs on each engine control card (`sig-light--tx`, `sig-light--rx`) only flash transiently when `fireSignalLight()` is called — which only triggers on specific route event states (`tx_state === "active"` or `rx_state === "received"`). Between events the lights revert to dim/off (default `opacity: 0.25`, `background: var(--muted)`).

**What's broken:**
- Lights don't maintain a persistent glow reflecting each engine's current routing state
- If no route events are flowing, all lights appear dead even if an engine is actively routing
- The `firing` class is removed after animation completes (`animationend` listener), so there's no steady-state indication

**Fix needed:** Add a persistent lit state (e.g. `.sig-light--tx.active`) that stays on as long as the engine has an active route, separate from the transient `firing` flash animation.

---

## Status

| # | Issue | Status | Notes |
|---|-------|--------|-------|
| 1 | Dropdown z-index | **Fixed** | `.chrome { z-index: 9999 }` — Gemini |
| 2 | Redundant gauge / workspace handler | **Open** | `#workspaceTachs` still renders duplicate tachs |
| 3 | Compact Context behavior | **Open** | Backend `/api/compact` needs archival + transcript summary injection |
| 4 | DSP TX/RX lights | **Fixed** | CSS `.active` classes added; SSE handler bug fixed (route was always undefined — reads `rest.target` from flat payload now) |
