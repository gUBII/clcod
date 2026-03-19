# Clark Kents Man Cave — Next Upgrade Plan

> Claude planned. Everyone adds. Codex allocates. Gemini executes.

---

## Feature Inventory

### 1. Dark / Light Mode Toggle

**What:** A two-mode theme system — dark (current) and light. A toggle button lives in the top chrome bar.

**How:**
- All colours live in CSS custom properties on `:root` already.
- Add a `[data-theme="light"]` selector on `<html>` with overridden variables (light bg, dark text, warm accents).
- Toggle button in `.chrome` writes `data-theme` to `<html>` and persists choice to `localStorage`.
- No JS frameworks, no build step — pure CSS + one JS listener.

**Files touched:** `web/styles.css`, `web/index.html`, `web/app.js`

---

### 2. Rename: clcod → Clark Kents Man Cave

**What:** Every visible string and internal key that says "clcod" becomes "Clark Kents Man Cave" (or abbreviated "CKMC" where space is tight).

**Scope:**
- `<title>` and `<h1>` in `index.html`
- `.chrome__eyebrow` label
- `localStorage` key `clcod.senderName` → `ckmc.senderName`
- Cookie name `clcod_session` → `ckmc_session` (both `supervisor.py` handler and JS)
- `supervisor.py` argparser description string
- Any `tmux` session name shown in the UI (attach command display only — not the actual session name, which is config-driven)

**Files touched:** `web/index.html`, `web/app.js`, `supervisor.py`

---

### 3. Tachometers — Bigger and Centred Above the Transcript

**What:** The three per-agent tachometers currently live in `.workspace__tachs` inside the workspace header (left column). Move them to the **top of the transcript panel**, centred horizontally, and scale them up (~80px diameter instead of 56px).

**How:**
- Remove `.workspace__tachs` from its current position in the header.
- Add a `<div class="tach-row" id="transcriptTachs">` at the top of `.panel--transcript` (above the transcript scroll area).
- Introduce `.tach--md` size class: dial 80px × 80px, needle 34px, visible half = 40px tall.
- Agent name label below each tach, state-coloured dot beside it.
- JS: move `workspaceTachs` target to `transcriptTachs`.

**Files touched:** `web/index.html`, `web/styles.css`, `web/app.js`

---

### 4. Sync Repo — Shared Workspace for All Agents

**What:** "Sync Repo" should do more than `git pull`. It should give every agent read/write access to the working directory so they can collaborate on the same files without permission errors.

**Full behaviour:**
1. `git pull` (already implemented in `/api/repo/pull`)
2. `chmod -R u+rw,g+rw .` on `SCRIPT_DIR` (makes all files group-writable)
3. Broadcast a `SYSTEM` message to the transcript: `"[SYNC] Repository synced. All agents now share read-write access to <path>."`
4. Return stdout/stderr of both operations in the API response.

**API change:** `/api/repo/pull` → expand to also run the chmod and inject the broadcast.

**Files touched:** `supervisor.py` (the `/api/repo/pull` handler), `web/app.js` (update status message to show path)

---

### 5. Compact Context — Smartest Agent Summarises

**What:** When "Compact Context" is pressed, the agent with the **most remaining capacity in its current 5-hour usage window** should be chosen to write the summary. That agent gets the summarisation request injected into the transcript.

**How:**
- Add a `usage_window` tracking object to the state store: `{ agent_name: { window_start: ts, tokens_used: int, limit: int } }`.
- Each time an agent posts a message (detected via transcript poll), increment `tokens_used` for that agent.
- When compact is triggered, pick the agent with `(limit - tokens_used)` maximised. Tie-break: alphabetical.
- Inject the summary request addressed to that agent: `"[COMPACT → CLAUDE] Please summarise…"` (or whichever wins).
- After 5 hours from `window_start`, reset `tokens_used` to 0 and `window_start` to now.

**Files touched:** `supervisor.py` (state, compact logic), `web/app.js` (pass winner name to compact response), `web/index.html` (optional: show which agent was chosen)

---

### 6. Fuel Gauge — 5-Hour Usage Window Display

**What:** Each agent card and control panel gets a **fuel gauge** showing `(remaining / limit)` for its current 5-hour usage window. This IS the usage limit. When it hits zero, that agent is out of fuel.

**Visual design:**
- Vertical bar gauge (think F1 fuel load indicator): full = green → amber → red at ≤20%.
- Sits to the right of the tachometer on each engine card and control card.
- Numeric readout: `87%` or `43 / 50k tok` beneath the bar.
- When an agent hits 0, its card shifts to `engine--error` state automatically (already styled).

**Data source:**
- `supervisor.py` tracks `tokens_used` per agent per window (see §5).
- `/api/state` response gains `fuel: { window_start, tokens_used, limit, pct_remaining }` per agent.
- JS reads `payload.fuel` and renders the gauge bar.

**Files touched:** `supervisor.py` (state shape), `web/app.js` (render fuel gauge), `web/styles.css` (`.fuel-gauge` styles), `web/index.html` (add gauge container to agent card template — generated in JS so HTML change minimal)

---

## Actionable Steps (Codex to Allocate)

| # | Task | Owner | Notes |
|---|------|-------|-------|
| A | Add `[data-theme]` CSS variables + light theme overrides to `styles.css` | **Gemini** | Pure CSS, self-contained |
| B | Add theme toggle button to chrome in `index.html` + localStorage persistence in `app.js` | **Gemini** | Depends on A |
| C | Rename all "clcod" → "CKMC / Clark Kents Man Cave" strings across all three files | **Codex** | Includes cookie name in `supervisor.py` |
| D | Create `.tach--md` size variant in `styles.css` and `tach-row` layout | **Gemini** | Pure CSS |
| E | Move tach rendering target from `workspaceTachs` → new `transcriptTachs` div; update `index.html` and `app.js` | **Gemini** | Depends on D |
| F | Expand `/api/repo/pull` in `supervisor.py` to run chmod + broadcast SYSTEM message | **Claude** | Backend only |
| G | Update sync button feedback in `app.js` to show broadcast confirmation | **Gemini** | Depends on F |
| H | Add `usage_window` tracking to `StateStore` and agent state shape in `supervisor.py` | **Claude** | Backend foundation for §5 and §6 |
| I | Wire compact logic to pick highest-remaining agent; update `/api/compact` response | **Claude** | Depends on H |
| J | Add `fuel` field to `/api/state` response per agent | **Claude** | Depends on H |
| K | Render fuel gauge in `app.js` (engine cards + control cards) | **Gemini** | Depends on J |
| L | Add `.fuel-gauge` CSS styles | **Gemini** | Depends on K; can be done in parallel with K |

---

## Open Questions (agents add below)

- **Token counting:** We don't have direct API token counts from agent processes. Proxy: count transcript messages per agent as a rough proxy, or read io_log line counts. What's the right unit for the fuel gauge?
- **Limit values:** What is the per-agent 5-hour token budget? Config-driven (add to `config.json`) or hardcoded per model?
- **Light mode palette:** Warm cream? Standard white? Should it match a specific brand vibe?
- **Cookie rename:** Renaming `clcod_session` to `ckmc_session` will log out all current sessions on deploy. Acceptable?
- **chmod scope:** Should chmod only apply to agent io_log dirs, or the full repo working dir? Full repo may be risky on shared machines.

---

*Allocation filled by Claude (Opus) since Codex described it verbally but didn't write it. Codex owns C + integration/regression verification. Claude owns backend (F, H, I, J). Gemini owns all UI/CSS (A, B, D, E, G, K, L). Codex — override anything you disagree with.*
