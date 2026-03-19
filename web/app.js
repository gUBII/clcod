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
const chatForm = document.getElementById("chatForm");
const senderName = document.getElementById("senderName");
const chatInput = document.getElementById("chatInput");
const chatStatus = document.getElementById("chatStatus");
const sendButton = document.getElementById("sendButton");

let unlocked = false;
let transcriptTimer = null;
let stateTimer = null;
let latestState = null;

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
  await pollTranscript();
});

/* Enter on the message input submits the form (default for single-line input in a form). */

function startPolling() {
  if (stateTimer) {
    clearInterval(stateTimer);
  }
  if (transcriptTimer) {
    clearInterval(transcriptTimer);
  }

  pollState();
  pollTranscript();
  stateTimer = setInterval(pollState, 1500);
  transcriptTimer = setInterval(pollTranscript, 2500);
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
  renderEngines(state.agents || {});

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

  for (const [name, payload] of entries) {
    const state = payload.state || "starting";
    const card = document.createElement("article");
    card.className = `engine engine--${state}`;
    card.innerHTML = `
      <div class="engine__spark"></div>
      <div class="engine__header">
        <span class="engine__name">${name}</span>
        <span class="engine__badge">${stateLabels[state] || state}</span>
      </div>
      <div class="engine__tach">
        <div class="tach__dial"></div>
        <div class="tach__needle"></div>
        <div class="tach__mark">${state === "ready" ? "REV" : state === "error" ? "ERR" : state === "warming" ? "REV" : "IDLE"}</div>
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

    const control = document.createElement("article");
    control.className = `control control--${state}`;
    control.dataset.agent = name;
    control.innerHTML = `
      <div class="control__header">
        <div>
          <p class="control__name">${name}</p>
          <p class="control__detail">${payload.pane_target || "no pane target"}</p>
        </div>
        <div class="control__tach">
          <div class="tach__dial tach__dial--sm"></div>
          <div class="tach__needle tach__needle--sm"></div>
          <div class="tach__mark tach__mark--sm">${state === "ready" ? "REV" : state === "error" ? "ERR" : state === "warming" ? "REV" : "IDLE"}</div>
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

function renderTranscript(entries) {
  transcript.innerHTML = "";
  if (!entries.length) {
    transcript.innerHTML = `<p class="transcript__empty">No transcript entries yet.</p>`;
    return;
  }
  for (const entry of entries) {
    const item = document.createElement("article");
    item.className = "message";
    item.innerHTML = `
      <header class="message__header">${entry.speaker}</header>
      <pre class="message__body"></pre>
    `;
    item.querySelector(".message__body").textContent = entry.text || "";
    transcript.appendChild(item);
  }
  transcript.scrollTop = transcript.scrollHeight;
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

showGate();
