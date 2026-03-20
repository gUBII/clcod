const stateLabels = {
  starting: "spinning up",
  auth: "tool online",
  warming: "routing traffic",
  ready: "ready",
  error: "no fuel left",
};

const stateNotes = {
  starting: "Preparing the local runtime and agent mirrors.",
  auth: "The CLI process is available and attached.",
  warming: "The room is settling and the engine is actively routing work.",
  ready: "Mirror and routing path are healthy.",
  error: "Out of fuel. The last routing cycle failed or capacity was exhausted.",
};

const previousStates = new Map();
const senderStorageKey = "clcod.senderName";

const gate = document.getElementById("gate");
const engineRoom = document.getElementById("engineRoom");
const workspace = document.getElementById("workspace");
const unlockForm = document.getElementById("unlockForm");
const unlockError = document.getElementById("unlockError");
const appPhase = document.getElementById("appPhase");
const relayState = document.getElementById("relayState");
const tmuxState = document.getElementById("tmuxState");
const workspaceRelay = document.getElementById("workspaceRelay");
const workspaceTmux = document.getElementById("workspaceTmux");
const tmuxCommand = document.getElementById("tmuxCommand");
const engineCards = document.getElementById("engineCards");
const statusGrid = document.getElementById("statusGrid");
const transcript = document.getElementById("transcript");
const copyTmux = document.getElementById("copyTmux");
const compactBtn = document.getElementById("compactBtn");
const syncRepoBtn = document.getElementById("syncRepoBtn");
const sleepBtn = document.getElementById("sleepBtn");
const workspaceTachs = document.getElementById("workspaceTachs");
const chatForm = document.getElementById("chatForm");
const senderName = document.getElementById("senderName");
const chatInput = document.getElementById("chatInput");
const chatStatus = document.getElementById("chatStatus");
const sendButton = document.getElementById("sendButton");
const mentionPopup = document.getElementById("mentionPopup");
const projectName = document.getElementById("projectName");
const themeToggle = document.getElementById("themeToggle");
const tasksPending = document.getElementById("tasksPending");
const tasksActive = document.getElementById("tasksActive");
const tasksDone = document.getElementById("tasksDone");
const dispatcherDot = document.getElementById("dispatcherDot");
const dispatcherLabel = document.getElementById("dispatcherLabel");
const dispatcherRoutes = document.getElementById("dispatcherRoutes");
const dispatcherAbsorbs = document.getElementById("dispatcherAbsorbs");
const dispatcherTokens = document.getElementById("dispatcherTokens");

let unlocked = false;
let transcriptTimer = null;
let stateTimer = null;
let latestState = null;
let lastSeenSeq = 0;
let lastSeenRev = 0;

unlockForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  unlockError.hidden = true;
  const form = new FormData(unlockForm);
  const password = String(form.get("password") || "");

  const response = await fetch("/api/unlock", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });

  if (!response.ok) {
    unlockError.hidden = false;
    unlockError.textContent = "Incorrect password. Check your local config or CLCOD_PASSWORD.";
    return;
  }

  unlocked = true;
  const payload = await response.json();
  renderState(payload.state);
  startPolling();
});

copyTmux.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(tmuxCommand.textContent || "");
    copyTmux.textContent = "Copied";
    setTimeout(() => {
      copyTmux.textContent = "Copy tmux command";
    }, 1200);
  } catch {
    copyTmux.textContent = "Copy failed";
    setTimeout(() => {
      copyTmux.textContent = "Copy tmux command";
    }, 1200);
  }
});

statusGrid.addEventListener("click", async (event) => {
  const restartButton = event.target.closest("button[data-agent][data-restart]");
  if (restartButton && !restartButton.disabled) {
    const agent = restartButton.dataset.agent;
    restartButton.disabled = true;
    restartButton.textContent = "Restarting...";
    try {
      const response = await fetch(`/api/agents/${agent}/restart`, { method: "POST" });
      if (response.ok) {
        restartButton.textContent = "Restarted";
      } else {
        restartButton.textContent = "Failed";
      }
    } catch {
      restartButton.textContent = "Error";
    }
    setTimeout(() => { restartButton.textContent = "Restart"; restartButton.disabled = false; }, 3000);
    return;
  }

  const detailsButton = event.target.closest("button[data-agent][data-details]");
  if (detailsButton) {
    openAgentModal(detailsButton.dataset.agent);
    return;
  }

  const inspectButton = event.target.closest("button[data-agent][data-inspect]");
  if (inspectButton && !inspectButton.disabled) {
    const paneTarget = inspectButton.dataset.inspect;
    const attachCmd = latestState?.tmux?.attach_command || "tmux attach -t triagent";
    const cmd = `tmux select-pane -t ${paneTarget} && ${attachCmd}`;
    try {
      await navigator.clipboard.writeText(cmd);
      inspectButton.textContent = "Copied";
      setTimeout(() => { inspectButton.textContent = "Inspect Stroker"; }, 1200);
    } catch {
      inspectButton.textContent = "Copy failed";
      setTimeout(() => { inspectButton.textContent = "Inspect Stroker"; }, 1200);
    }
    return;
  }

  const button = event.target.closest("button[data-agent][data-kind][data-option]");
  if (!button || button.disabled) {
    return;
  }

  const agent = button.dataset.agent;
  const kind = button.dataset.kind;
  const option = button.dataset.option;
  const payload = latestState?.agents?.[agent];
  if (!payload) {
    return;
  }

  const body = {
    selected_model: kind === "model" ? option : payload.selected_model,
    selected_effort: kind === "effort" ? option : payload.selected_effort,
  };

  setControlMessage(agent, `Updating ${kind}...`);
  const response = await fetch(`/api/agents/${agent}/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    setControlMessage(agent, "Update failed.");
    return;
  }

  const result = await response.json();
  renderState(result.state);
  setControlMessage(agent, `${kind} set to ${option}.`);
});

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = String(senderName.value || "").trim();
  const message = String(chatInput.value || "").trim();
  if (!name || !message) {
    chatStatus.textContent = "Sender and message are required.";
    return;
  }

  sendButton.disabled = true;
  chatStatus.textContent = "Writing message into the room...";
  localStorage.setItem(senderStorageKey, name);

  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, message }),
  });

  sendButton.disabled = false;
  if (!response.ok) {
    chatStatus.textContent = "Message failed.";
    return;
  }

  const result = await response.json();
  chatInput.value = "";
  chatStatus.textContent = "Message posted to the transcript.";
  renderState(result.state);
  setTimeout(() => { pollTranscript(); }, 500);
});

/* ── @ mention popup ─────────────────────────────────── */

const STATIC_MENTION_TARGETS = ["CLAUDE", "CODEX", "GEMINI"];

function getMentionTargets() {
  const agentNames = latestState?.agents ? Object.keys(latestState.agents).map(n => n.toUpperCase()) : [];
  const merged = new Set([...STATIC_MENTION_TARGETS, ...agentNames]);
  return Array.from(merged);
}

function getMentionQuery(value, cursorPos) {
  const before = value.slice(0, cursorPos);
  const match = before.match(/@(\w*)$/);
  return match ? match[1] : null;
}

function showMentionPopup(query) {
  const targets = getMentionTargets();
  const filtered = query === ""
    ? targets
    : targets.filter(t => t.toLowerCase().startsWith(query.toLowerCase()));

  if (!filtered.length) {
    hideMentionPopup();
    return;
  }

  mentionPopup.innerHTML = "";
  filtered.forEach((name, i) => {
    const item = document.createElement("div");
    item.className = "mention-popup__item";
    item.dataset.name = name;
    if (i === 0) item.classList.add("active");
    item.textContent = `@${name}`;
    item.addEventListener("mousedown", (e) => {
      e.preventDefault();
      insertMention(name);
    });
    mentionPopup.appendChild(item);
  });
  mentionPopup.classList.remove("hidden");
}

function hideMentionPopup() {
  mentionPopup.classList.add("hidden");
  mentionPopup.innerHTML = "";
}

function getActiveMentionItem() {
  return mentionPopup.querySelector(".active");
}

function moveMentionSelection(dir) {
  const items = Array.from(mentionPopup.querySelectorAll(".mention-popup__item"));
  if (!items.length) return;
  const current = getActiveMentionItem();
  const idx = current ? items.indexOf(current) : -1;
  const next = items[(idx + dir + items.length) % items.length];
  if (current) current.classList.remove("active");
  next.classList.add("active");
}

function insertMention(name) {
  const value = chatInput.value;
  const pos = chatInput.selectionStart;
  const before = value.slice(0, pos).replace(/@\w*$/, `@${name} `);
  const after = value.slice(pos);
  chatInput.value = before + after;
  chatInput.setSelectionRange(before.length, before.length);
  hideMentionPopup();
  chatInput.focus();
}

chatInput.addEventListener("input", () => {
  const query = getMentionQuery(chatInput.value, chatInput.selectionStart);
  if (query === null) {
    hideMentionPopup();
  } else {
    showMentionPopup(query);
  }
});

chatInput.addEventListener("keydown", (e) => {
  if (mentionPopup.classList.contains("hidden")) return;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    moveMentionSelection(1);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    moveMentionSelection(-1);
  } else if (e.key === "Enter" || e.key === "Tab") {
    const active = getActiveMentionItem();
    if (active) {
      e.preventDefault();
      insertMention(active.dataset.name);
    }
  } else if (e.key === "Escape") {
    hideMentionPopup();
  }
});

chatInput.addEventListener("blur", () => {
  setTimeout(hideMentionPopup, 150);
});

/* Enter on the message input submits the form (default for single-line input in a form). */

compactBtn.addEventListener("click", async () => {
  compactBtn.disabled = true;
  compactBtn.textContent = "Compacting...";
  try {
    const response = await fetch("/api/compact", { method: "POST" });
    const result = await response.json();
    if (result.ok) {
      compactBtn.textContent = "Compacted";
      await pollTranscript();
      setTimeout(() => { compactBtn.textContent = "Compact Context"; }, 3000);
    } else {
      compactBtn.textContent = "Failed";
      setTimeout(() => { compactBtn.textContent = "Compact Context"; }, 3000);
    }
  } catch {
    compactBtn.textContent = "Error";
    setTimeout(() => { compactBtn.textContent = "Compact Context"; }, 3000);
  } finally {
    compactBtn.disabled = false;
  }
});

sleepBtn.addEventListener("click", async () => {
  sleepBtn.disabled = true;
  const isSleeping = latestState?.app?.sleeping;
  sleepBtn.textContent = isSleeping ? "Waking..." : "Sleeping...";
  try {
    const response = await fetch("/api/sleep", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sleep: !isSleeping }),
    });
    const result = await response.json();
    if (result.ok) {
      renderState(result.state);
      sleepBtn.textContent = result.sleeping ? "Wake" : "Sleep";
    } else {
      sleepBtn.textContent = "Failed";
      setTimeout(() => { sleepBtn.textContent = isSleeping ? "Wake" : "Sleep"; }, 3000);
    }
  } catch {
    sleepBtn.textContent = "Error";
    setTimeout(() => { sleepBtn.textContent = isSleeping ? "Wake" : "Sleep"; }, 3000);
  } finally {
    sleepBtn.disabled = false;
  }
});

syncRepoBtn.addEventListener("click", async () => {
  syncRepoBtn.disabled = true;
  syncRepoBtn.textContent = "Pulling...";
  try {
    const response = await fetch("/api/repo/pull", { method: "POST" });
    const result = await response.json();
    if (result.ok) {
      syncRepoBtn.textContent = "Synced";
      setTimeout(() => { syncRepoBtn.textContent = "Sync Repo"; }, 3000);
    } else {
      syncRepoBtn.textContent = "Failed";
      setTimeout(() => { syncRepoBtn.textContent = "Sync Repo"; }, 3000);
    }
  } catch {
    syncRepoBtn.textContent = "Error";
    setTimeout(() => { syncRepoBtn.textContent = "Sync Repo"; }, 3000);
  } finally {
    syncRepoBtn.disabled = false;
  }
});

let evtSource = null;

function startPolling() {
  if (stateTimer) {
    clearInterval(stateTimer);
  }
  if (transcriptTimer) {
    clearInterval(transcriptTimer);
  }
  if (evtSource) {
    evtSource.close();
    evtSource = null;
  }

  evtSource = new EventSource("/api/events");

  evtSource.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      switch (data.type) {
        case "init":
        case "state_refresh":
          if (data.locked) {
            unlocked = false;
            showGate();
          } else {
            renderState(data);
          }
          break;
        case "relay_state":
          if (latestState) {
            latestState.relay = { ...latestState.relay, state: data.state };
            renderState(latestState);
          }
          break;
        case "transcript":
          // Update rev tracking
          if (data.rev !== undefined && data.rev > lastSeenRev) {
            // Detect gap: if rev jumped by more than 1, we missed events — full refresh
            const gap = data.rev - lastSeenRev > 1;
            lastSeenRev = data.rev;
            if (gap) {
              pollTranscript();
              break;
            }
          }
          // Primary path: instant append when message payload is present
          if (data.message && data.message.seq > lastSeenSeq) {
            appendMessage(data.message);
            lastSeenSeq = data.message.seq;
          } else if (!data.message) {
            // No message payload — reconcile via full fetch
            pollTranscript();
          }
          break;
        case "agent_state":
          if (latestState?.agents?.[data.agent]) {
            latestState.agents[data.agent].state = data.state;
            if (data.last_error !== undefined) {
              latestState.agents[data.agent].last_error = data.last_error;
            }
            renderState(latestState);
          }
          break;
        case "dispatcher":
          if (latestState) {
            const { type: _t, ...rest } = data;
            latestState.dispatcher = { ...latestState.dispatcher, ...rest };
            renderDispatcher(latestState);
          }
          break;
        case "task_created":
          fetchTasks();
          break;
        case "tasks_updated":
          fetchTasks();
          break;
        case "task_updated":
          fetchTasks();
          break;
        case "tasks_cleared":
          renderTaskBoard([]);
          break;
      }
    } catch { /* ignore malformed */ }
  };

  evtSource.onerror = () => {
    evtSource.close();
    evtSource = null;
    // Fall back to polling
    pollState();
    pollTranscript();
    stateTimer = setInterval(pollState, 1500);
    transcriptTimer = setInterval(pollTranscript, 2500);
  };

  // Fetch transcript and tasks once on connect; SSE events trigger refreshes
  pollTranscript();
  fetchTasks();
}

async function pollState() {
  const response = await fetch("/api/state");
  const payload = await response.json();
  if (payload.locked) {
    unlocked = false;
    showGate();
    return;
  }
  renderState(payload);
}

async function pollTranscript() {
  if (!unlocked) {
    return;
  }
  const response = await fetch("/api/transcript?limit=120");
  if (!response.ok) {
    return;
  }
  const payload = await response.json();
  renderTranscript(payload.entries || []);
}

function renderState(state) {
  latestState = state;
  const phase = state.app?.phase || "booting";
  appPhase.textContent = phase.toUpperCase();
  relayState.textContent = `relay: ${state.relay?.state || "unknown"}`;
  tmuxState.textContent = `tmux: ${state.tmux?.state || "unknown"}`;
  workspaceRelay.textContent = relayState.textContent;
  workspaceTmux.textContent = tmuxState.textContent;
  tmuxCommand.textContent = state.tmux?.attach_command || "tmux attach -t triagent";
  hydrateSender(state.app?.default_sender || "Operator");
  sleepBtn.textContent = state.app?.sleeping ? "Wake" : "Sleep";
  renderEngines(state.agents || {});
  renderProjectName(state);
  renderDispatcher(state);

  // Update transcript revision from initial state
  if (state.transcript?.rev !== undefined && state.transcript.rev > lastSeenRev) {
    lastSeenRev = state.transcript.rev;
  }

  if (!unlocked) {
    showGate();
    return;
  }
  if (phase === "ready") {
    showWorkspace();
  } else {
    showEngineRoom();
  }
}

function hydrateSender(defaultSender) {
  const remembered = localStorage.getItem(senderStorageKey);
  if (!senderName.value.trim()) {
    senderName.value = remembered || defaultSender;
  }
}

function labelForOption(options, optionId, fallback = "default") {
  const match = (options || []).find((option) => option.id === optionId);
  return match?.label || fallback;
}

function computePressure(payload) {
  const fuel = payload.fuel || {};
  const pressure = payload.pressure || {};
  const fuelPct = fuel.pct_remaining != null ? fuel.pct_remaining : 100;
  const queueDepth = pressure.queue_depth || 0;
  const latencyMs = pressure.last_latency_ms || 0;
  const tps = pressure.tokens_per_sec || 0;
  const errorRate = pressure.error_rate_5m || 0;

  // Weighted pressure score 0-100
  const fuelPressure = Math.max(0, (100 - fuelPct)) * 0.25;
  const queuePressure = Math.min(queueDepth * 20, 30);
  const latencyPressure = Math.min(latencyMs / 400, 25);
  const errorPressure = Math.min(errorRate * 8, 20);
  const score = Math.min(100, Math.round(fuelPressure + queuePressure + latencyPressure + errorPressure));

  // Map 0-100 to needle angle: -90deg (idle) to +90deg (redline)
  const angle = -90 + (score / 100) * 180;
  const level = score > 70 ? "high" : score > 35 ? "mid" : "low";
  return { score, angle, level, fuelPct };
}

function needleColor(level) {
  if (level === "high") return "var(--red, #ef4444)";
  if (level === "mid") return "var(--amber, #f59e0b)";
  return "var(--blue, #6cb4ee)";
}

function allowedEffortIds(payload) {
  const matrix = payload.effort_matrix || {};
  const selectedModel = payload.selected_model || "default";
  const fromMatrix = matrix[selectedModel] || matrix.default;
  if (Array.isArray(fromMatrix) && fromMatrix.length > 0) {
    return new Set(fromMatrix);
  }
  return new Set((payload.effort_options || []).map((option) => option.id));
}

function renderEngines(agents) {
  const entries = Object.entries(agents);
  engineCards.innerHTML = "";
  statusGrid.innerHTML = "";
  workspaceTachs.innerHTML = "";

  for (const [name, payload] of entries) {
    const state = payload.state || "starting";
    const p = computePressure(payload);
    const nColor = needleColor(p.level);
    const fuelAngle = (p.fuelPct / 100) * 180;
    const card = document.createElement("article");
    card.className = `engine engine--${state}`;
    card.dataset.pressure = p.level;
    card.innerHTML = `
      <div class="engine__spark"></div>
      <div class="engine__header">
        <span class="engine__name">${name}</span>
        <span class="engine__badge">${stateLabels[state] || state}</span>
      </div>
      <div class="engine__tach">
        <div class="tach__dial" style="--fuel-angle:${fuelAngle}deg"></div>
        <div class="tach__fuel-arc" style="--fuel-angle:${fuelAngle}deg"></div>
        <div class="tach__needle" style="transform:translateX(-50%) rotate(${p.angle}deg);--needle-color:${nColor}"></div>
        <div class="tach__mark">${state === "error" ? "ERR" : p.score > 70 ? "HOT" : p.score > 35 ? "REV" : "IDLE"}</div>
      </div>
      <p class="engine__note">${stateNotes[state] || ""}</p>
      <div class="engine__meta engine__meta--stack">
        <span>mirror: ${(payload.mirror_view || payload.mirror_mode || "log").toUpperCase()}</span>
        <span>model: ${labelForOption(payload.model_options, payload.selected_model, "default")}</span>
        <span>effort: ${labelForOption(payload.effort_options, payload.selected_effort, "default")}</span>
      </div>
    `;

    const previous = previousStates.get(name);
    if (previous && previous !== state) {
      card.classList.add("engine--ignite");
    }
    previousStates.set(name, state);
    engineCards.appendChild(card);

    const htach = document.createElement("div");
    htach.className = `htach control--${state}`;
    htach.dataset.pressure = p.level;
    htach.innerHTML = `
      <span class="htach__name">${name}</span>
      <div class="control__tach">
        <div class="tach__dial tach__dial--sm" style="--fuel-angle:${fuelAngle}deg"></div>
        <div class="tach__fuel-arc tach__fuel-arc--sm" style="--fuel-angle:${fuelAngle}deg"></div>
        <div class="tach__needle tach__needle--sm" style="transform:translateX(-50%) rotate(${p.angle}deg);--needle-color:${nColor}"></div>
        <div class="tach__mark tach__mark--sm">${state === "error" ? "ERR" : p.score > 70 ? "HOT" : p.score > 35 ? "REV" : "IDLE"}</div>
      </div>
    `;
    workspaceTachs.appendChild(htach);

    const control = document.createElement("article");
    control.className = `control control--${state}`;
    control.dataset.agent = name;
    control.dataset.pressure = p.level;
    control.innerHTML = `
      <div class="control__header">
        <div>
          <p class="control__name">${name}</p>
          <p class="control__detail">${payload.pane_target || "no pane target"}</p>
        </div>
        <div class="control__tach">
          <div class="tach__dial tach__dial--sm" style="--fuel-angle:${fuelAngle}deg"></div>
          <div class="tach__fuel-arc tach__fuel-arc--sm" style="--fuel-angle:${fuelAngle}deg"></div>
          <div class="tach__needle tach__needle--sm" style="transform:translateX(-50%) rotate(${p.angle}deg);--needle-color:${nColor}"></div>
          <div class="tach__mark tach__mark--sm">${state === "error" ? "ERR" : p.score > 70 ? "HOT" : p.score > 35 ? "REV" : "IDLE"}</div>
        </div>
        <div class="control__status">
          <span>${stateLabels[state] || state}</span>
          <span>${(payload.mirror_view || payload.mirror_mode || "log").toUpperCase()}</span>
        </div>
      </div>
      <div class="control__section">
        <p class="control__label">Model</p>
        <div class="chip-row">
          ${renderChoiceButtons(name, "model", payload.model_options || [], payload.selected_model || "default")}
        </div>
      </div>
      <div class="control__section">
        <p class="control__label">Effort</p>
        <div class="chip-row">
          ${renderChoiceButtons(
            name,
            "effort",
            (payload.effort_options || []).filter((option) => allowedEffortIds(payload).has(option.id)),
            payload.selected_effort || "default",
          )}
        </div>
      </div>
      ${(() => {
        const f = payload.fuel || {};
        const pct = f.pct_remaining != null ? f.pct_remaining : 100;
        const used = f.tokens_used || 0;
        const limit = f.limit || 0;
        const remaining = f.remaining != null ? f.remaining : limit;
        const color = pct > 50 ? "var(--green, #22c55e)" : pct > 20 ? "var(--amber, #f59e0b)" : "var(--red, #ef4444)";
        return `<div class="control__section">
          <p class="control__label">Fuel &mdash; ${Math.round(pct)}% remaining</p>
          <div class="fuel-gauge">
            <div class="fuel-gauge__bar" style="width:${pct}%;background:${color}"></div>
          </div>
          <p class="control__detail">${used.toLocaleString()} / ${limit.toLocaleString()} tokens used</p>
        </div>`;
      })()}
      <div class="control__section control__section--actions">
        <button
          type="button"
          class="compact-btn"
          data-agent="${name}"
          data-inspect="${payload.pane_target || ""}"
          ${payload.pane_target ? "" : "disabled"}
          title="${payload.pane_target ? `Copy: tmux select-pane -t ${payload.pane_target} &amp;&amp; ${latestState?.tmux?.attach_command || "tmux attach -t triagent"}` : "No pane target registered"}"
        >Inspect Stroker</button>
        <button
          type="button"
          class="compact-btn restart-btn"
          data-agent="${name}"
          data-restart="true"
          title="Kill and restart this agent's mirror process"
        >Restart</button>
        <button
          type="button"
          class="compact-btn"
          data-agent="${name}"
          data-details="true"
          title="View detailed agent state, pressure, and logs"
        >Details</button>
      </div>
      <p class="control__message" data-control-message>${payload.last_error || ""}</p>
    `;
    statusGrid.appendChild(control);
  }
}

function renderChoiceButtons(agent, kind, options, selectedId) {
  if (!options || options.length === 0) {
    return `<span class="chip chip--empty">Not supported</span>`;
  }
  return options
    .map((option) => {
      const active = option.id === selectedId;
      return `
        <button
          type="button"
          class="chip ${active ? "chip--active" : ""}"
          data-agent="${agent}"
          data-kind="${kind}"
          data-option="${option.id}"
          aria-pressed="${active ? "true" : "false"}"
          title="${option.description || option.label}"
        >${option.label}</button>
      `;
    })
    .join("");
}

function setControlMessage(agent, message) {
  const card = statusGrid.querySelector(`[data-agent="${agent}"]`);
  const target = card?.querySelector("[data-control-message]");
  if (target) {
    target.textContent = message;
  }
}

function formatTime(ts) {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    if (isNaN(d)) return "";
    let h = d.getHours();
    const m = String(d.getMinutes()).padStart(2, "0");
    const ampm = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    return `${h}:${m} ${ampm}`;
  } catch {
    return "";
  }
}

function renderTranscript(entries) {
  transcript.innerHTML = "";
  if (!entries.length) {
    transcript.innerHTML = `<p class="transcript__empty">No transcript entries yet.</p>`;
    return;
  }
  for (const entry of entries) {
    const time = formatTime(entry.ts);
    const timeSpan = time ? `<span class="message__time">${time}</span>` : "";
    const item = document.createElement("article");
    item.className = "message";
    item.innerHTML = `
      <header class="message__header">${entry.speaker} ${timeSpan}</header>
      <pre class="message__body"></pre>
    `;
    item.querySelector(".message__body").textContent = entry.text || "";
    transcript.appendChild(item);
    if (entry.seq && entry.seq > lastSeenSeq) {
      lastSeenSeq = entry.seq;
    }
  }
  transcript.scrollTop = transcript.scrollHeight;
}

function appendMessage(msg) {
  // Remove "no entries" placeholder if present
  const empty = transcript.querySelector(".transcript__empty");
  if (empty) empty.remove();

  const time = formatTime(msg.ts);
  const timeSpan = time ? `<span class="message__time">${time}</span>` : "";
  const item = document.createElement("article");
  item.className = "message";
  item.innerHTML = `
    <header class="message__header">${escapeHtml(msg.sender)} ${timeSpan}</header>
    <pre class="message__body"></pre>
  `;
  item.querySelector(".message__body").textContent = msg.body || "";
  transcript.appendChild(item);
  transcript.scrollTop = transcript.scrollHeight;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function showGate() {
  gate.classList.remove("hidden");
  engineRoom.classList.add("hidden");
  workspace.classList.add("hidden");
}

function showEngineRoom() {
  gate.classList.add("hidden");
  engineRoom.classList.remove("hidden");
  workspace.classList.add("hidden");
}

function showWorkspace() {
  gate.classList.add("hidden");
  engineRoom.classList.add("hidden");
  workspace.classList.remove("hidden");
}

/* ── Project name + picker ────────────────────────────── */

const projectMenuBtn = document.getElementById("projectMenuBtn");
const projectDropdown = document.getElementById("projectDropdown");
const projectPathInput = document.getElementById("projectPathInput");
const lockPathBtn = document.getElementById("lockPathBtn");
const projectUrlInput = document.getElementById("projectUrlInput");
const cloneUrlBtn = document.getElementById("cloneUrlBtn");
const unlockProjectBtn = document.getElementById("unlockProjectBtn");
const projectList = document.getElementById("projectList");

projectMenuBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  projectDropdown.classList.toggle("hidden");
  if (!projectDropdown.classList.contains("hidden")) {
    fetchProjects();
  }
});

document.addEventListener("click", (e) => {
  if (!projectDropdown.contains(e.target) && e.target !== projectMenuBtn) {
    projectDropdown.classList.add("hidden");
  }
});

lockPathBtn.addEventListener("click", async () => {
  const path = projectPathInput.value.trim();
  if (!path) return;
  lockPathBtn.disabled = true;
  lockPathBtn.textContent = "Locking...";
  try {
    const res = await fetch("/api/projects/lock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const data = await res.json();
    if (data.ok) {
      projectPathInput.value = "";
      projectDropdown.classList.add("hidden");
      pollState();
    } else {
      lockPathBtn.textContent = data.error || "Failed";
      setTimeout(() => { lockPathBtn.textContent = "Lock"; }, 3000);
    }
  } catch {
    lockPathBtn.textContent = "Error";
    setTimeout(() => { lockPathBtn.textContent = "Lock"; }, 3000);
  } finally {
    lockPathBtn.disabled = false;
    lockPathBtn.textContent = "Lock";
  }
});

cloneUrlBtn.addEventListener("click", async () => {
  const url = projectUrlInput.value.trim();
  if (!url) return;
  cloneUrlBtn.disabled = true;
  cloneUrlBtn.textContent = "Cloning...";
  try {
    const res = await fetch("/api/projects/lock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (data.ok) {
      projectUrlInput.value = "";
      projectDropdown.classList.add("hidden");
      pollState();
    } else {
      cloneUrlBtn.textContent = data.error || "Failed";
      setTimeout(() => { cloneUrlBtn.textContent = "Clone"; }, 3000);
    }
  } catch {
    cloneUrlBtn.textContent = "Error";
    setTimeout(() => { cloneUrlBtn.textContent = "Clone"; }, 3000);
  } finally {
    cloneUrlBtn.disabled = false;
    cloneUrlBtn.textContent = "Clone";
  }
});

unlockProjectBtn.addEventListener("click", async () => {
  unlockProjectBtn.disabled = true;
  unlockProjectBtn.textContent = "Unlocking...";
  try {
    const res = await fetch("/api/projects/unlock", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      projectDropdown.classList.add("hidden");
      pollState();
    }
  } catch { /* ignore */ }
  finally {
    unlockProjectBtn.disabled = false;
    unlockProjectBtn.textContent = "Unlock (return home)";
  }
});

async function fetchProjects() {
  try {
    const res = await fetch("/api/projects");
    if (!res.ok) return;
    const data = await res.json();
    const projects = data.projects || {};
    const activeId = data.active;
    projectList.innerHTML = "";
    const ids = Object.keys(projects);
    if (!ids.length) {
      projectList.innerHTML = `<p class="project-dropdown__empty">No saved projects</p>`;
      return;
    }
    for (const id of ids) {
      const p = projects[id];
      const row = document.createElement("div");
      row.className = `project-dropdown__item${id === activeId ? " project-dropdown__item--active" : ""}`;
      row.innerHTML = `<span>${p.name || id}</span><button type="button" class="compact-btn" data-lock-id="${id}" data-lock-path="${p.path}">Lock</button>`;
      projectList.appendChild(row);
    }
    projectList.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-lock-path]");
      if (!btn) return;
      btn.disabled = true;
      btn.textContent = "Locking...";
      try {
        const res = await fetch("/api/projects/lock", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: btn.dataset.lockPath }),
        });
        if (res.ok) {
          projectDropdown.classList.add("hidden");
          pollState();
        }
      } catch { /* ignore */ }
      finally { btn.disabled = false; btn.textContent = "Lock"; }
    });
  } catch { /* ignore */ }
}

function renderProjectName(state) {
  const name = state.project?.name || state.project?.active || "none";
  projectName.textContent = name;
}

/* ── Dispatcher modal ─────────────────────────────────── */

const dispatcherModal = document.getElementById("dispatcherModal");
const dispatcherModalBody = document.getElementById("dispatcherModalBody");

document.getElementById("dispatcherBar").addEventListener("click", async () => {
  dispatcherModal.classList.remove("hidden");
  dispatcherModalBody.innerHTML = "Loading...";
  try {
    const res = await fetch("/api/dispatcher/health");
    if (!res.ok) { dispatcherModalBody.innerHTML = "Failed to load."; return; }
    const d = await res.json();
    const ollamaStatus = d.available
      ? `<span style="color:var(--ready)">Online</span>`
      : `<span style="color:var(--error)">Offline</span>`;
    const modelsList = (d.models || []).length
      ? d.models.map(m => `<div class="modal-kv"><span>${m}</span></div>`).join("")
      : `<span style="color:var(--muted)">No models loaded</span>`;
    dispatcherModalBody.innerHTML = `
      <div class="modal-section">
        <p class="modal-section__title">Ollama Status</p>
        <div class="modal-kv"><span>Status</span><span>${ollamaStatus}</span></div>
        <div class="modal-kv"><span>Router model</span><span>${d.router_model || "none"}</span></div>
        <div class="modal-kv"><span>Dispatcher state</span><span>${d.state || "unknown"}</span></div>
      </div>
      <div class="modal-section">
        <p class="modal-section__title">Loaded Models</p>
        ${modelsList}
      </div>
      <div class="modal-section">
        <p class="modal-section__title">Routing Stats</p>
        <div class="modal-kv"><span>Routes</span><span>${d.routes_total || 0}</span></div>
        <div class="modal-kv"><span>Absorbed</span><span>${d.absorbs_total || 0}</span></div>
        <div class="modal-kv"><span>Tokens saved</span><span>${(d.tokens_saved || 0).toLocaleString()}</span></div>
        <div class="modal-kv"><span>Last action</span><span>${d.last_action || "—"}</span></div>
        <div class="modal-kv"><span>Last targets</span><span>${(d.last_targets || []).join(", ") || "—"}</span></div>
      </div>
    `;
  } catch { dispatcherModalBody.innerHTML = "Error fetching health."; }
});

dispatcherModal.querySelector(".modal-panel__close").addEventListener("click", () => {
  dispatcherModal.classList.add("hidden");
});
dispatcherModal.addEventListener("click", (e) => {
  if (e.target === dispatcherModal) dispatcherModal.classList.add("hidden");
});

/* ── Agent modal ─────────────────────────────────────── */

const agentModal = document.getElementById("agentModal");
const agentModalHeader = document.getElementById("agentModalHeader");
const agentModalBody = document.getElementById("agentModalBody");

async function openAgentModal(name) {
  agentModal.classList.remove("hidden");
  agentModalHeader.textContent = name;
  agentModalBody.innerHTML = "Loading...";

  const snap = latestState?.agents?.[name] || {};
  const pressure = snap.pressure || {};
  const fuel = snap.fuel || {};

  let warnings = "";
  if (snap.state === "warming" && snap.last_reply_at) {
    const silent = (Date.now() - new Date(snap.last_reply_at).getTime()) / 1000;
    if (silent > 30) {
      warnings += `<div class="modal-warning">Silent for ${Math.round(silent)}s while warming — may be stuck</div>`;
    }
  }
  if ((pressure.queue_depth || 0) > 3) {
    warnings += `<div class="modal-warning">Queue depth is ${pressure.queue_depth} — backpressure building</div>`;
  }

  let html = warnings;
  html += `
    <div class="modal-section">
      <p class="modal-section__title">State</p>
      <div class="modal-kv"><span>State</span><span>${snap.state || "unknown"}</span></div>
      <div class="modal-kv"><span>Session</span><span>${snap.session_id || "—"}</span></div>
      <div class="modal-kv"><span>Last reply</span><span>${snap.last_reply_at || "—"}</span></div>
      <div class="modal-kv"><span>Last error</span><span>${snap.last_error || "—"}</span></div>
      <div class="modal-kv"><span>Pane target</span><span>${snap.pane_target || "—"}</span></div>
    </div>
    <div class="modal-section">
      <p class="modal-section__title">Pressure</p>
      <div class="modal-kv"><span>Queue depth</span><span>${pressure.queue_depth || 0}</span></div>
      <div class="modal-kv"><span>Latency</span><span>${pressure.last_latency_ms || 0} ms</span></div>
      <div class="modal-kv"><span>Tokens/sec</span><span>${pressure.tokens_per_sec || 0}</span></div>
      <div class="modal-kv"><span>Error rate (5m)</span><span>${pressure.error_rate_5m || 0}</span></div>
    </div>
    <div class="modal-section">
      <p class="modal-section__title">Fuel</p>
      <div class="modal-kv"><span>Remaining</span><span>${fuel.pct_remaining != null ? Math.round(fuel.pct_remaining) + "%" : "—"}</span></div>
      <div class="modal-kv"><span>Tokens used</span><span>${(fuel.tokens_used || 0).toLocaleString()}</span></div>
      <div class="modal-kv"><span>Limit</span><span>${(fuel.limit || 0).toLocaleString()}</span></div>
    </div>
  `;

  agentModalBody.innerHTML = html + `<div class="modal-section"><p class="modal-section__title">IO Log (last 30)</p><pre class="modal-log">Loading...</pre></div>`;

  try {
    const res = await fetch(`/api/agents/${encodeURIComponent(name)}/logs?tail=30`);
    if (res.ok) {
      const data = await res.json();
      const logEl = agentModalBody.querySelector(".modal-log");
      logEl.textContent = (data.lines || []).join("\n") || "(empty)";
    }
  } catch { /* ignore */ }
}

agentModal.querySelector(".modal-panel__close").addEventListener("click", () => {
  agentModal.classList.add("hidden");
});
agentModal.addEventListener("click", (e) => {
  if (e.target === agentModal) agentModal.classList.add("hidden");
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    dispatcherModal.classList.add("hidden");
    agentModal.classList.add("hidden");
  }
});

/* ── Dispatcher bar ──────────────────────────────────── */

function renderDispatcher(state) {
  const d = state.dispatcher || {};
  const dState = d.state || "disabled";
  dispatcherDot.className = `dispatcher-bar__dot dispatcher-bar__dot--${dState}`;
  const modelInfo = d.router_model ? ` (${d.router_model})` : "";
  dispatcherLabel.textContent = `Dispatcher: ${dState}${modelInfo}`;
  dispatcherRoutes.textContent = String(d.routes_total || 0);
  dispatcherAbsorbs.textContent = String(d.absorbs_total || 0);
  dispatcherTokens.textContent = (d.tokens_saved || 0).toLocaleString();
}

/* ── Task board ──────────────────────────────────────── */

let cachedTasks = [];

function renderTaskBoard(tasks) {
  cachedTasks = tasks;
  const pending = tasks.filter(t => t.status === "pending");
  const active = tasks.filter(t => t.status === "assigned" || t.status === "in_progress");
  const done = tasks.filter(t => t.status === "done").slice(-20);

  tasksPending.innerHTML = pending.length ? "" : `<p class="task-board__empty">—</p>`;
  tasksActive.innerHTML = active.length ? "" : `<p class="task-board__empty">—</p>`;
  tasksDone.innerHTML = done.length ? "" : `<p class="task-board__empty">—</p>`;

  for (const t of pending) tasksPending.appendChild(taskCard(t));
  for (const t of active) tasksActive.appendChild(taskCard(t));
  for (const t of done) tasksDone.appendChild(taskCard(t));
}

function taskCard(t) {
  const el = document.createElement("div");
  el.className = `task-card task-card--${t.status}`;
  if (t.id != null) {
    const idBadge = document.createElement("span");
    idBadge.className = "task-card__id";
    idBadge.textContent = `#${t.id}`;
    el.appendChild(idBadge);
  }
  const title = document.createElement("p");
  title.className = "task-card__title";
  title.textContent = t.title || t.id || "Untitled";
  el.appendChild(title);
  if (t.assigned_to?.length) {
    const meta = document.createElement("p");
    meta.className = "task-card__meta";
    meta.textContent = t.assigned_to.join(", ");
    el.appendChild(meta);
  }
  return el;
}

async function fetchTasks() {
  try {
    const res = await fetch("/api/tasks");
    if (!res.ok) return;
    const data = await res.json();
    renderTaskBoard(data.tasks || []);
  } catch { /* ignore */ }
}

/* ── Theme toggle ────────────────────────────────────── */

const themeKey = "clcod.theme";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem(themeKey, theme);
  themeToggle.textContent = theme === "dark" ? "◐" : "◑";
}

themeToggle.addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  applyTheme(current === "dark" ? "light" : "dark");
});

applyTheme(localStorage.getItem(themeKey) || "dark");

(async () => {
  try {
    const res = await fetch("/api/state");
    if (res.ok) {
      const data = await res.json();
      if (!data.locked) {
        unlocked = true;
        renderState(data);
        startPolling();
        return;
      }
    }
  } catch { /* fall through */ }
  showGate();
})();
