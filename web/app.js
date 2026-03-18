const stateLabels = {
  starting: "spinning up",
  auth: "tool online",
  warming: "routing traffic",
  ready: "ready",
  error: "fault",
};

const stateNotes = {
  starting: "Preparing the local runtime and agent mirrors.",
  auth: "The CLI process is available and attached.",
  warming: "The room is settling and the engine is actively routing work.",
  ready: "Mirror and routing path are healthy.",
  error: "The last routing cycle or mirror command failed.",
};

const previousStates = new Map();

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

let unlocked = false;
let transcriptTimer = null;
let stateTimer = null;

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
  const phase = state.app?.phase || "booting";
  appPhase.textContent = phase.toUpperCase();
  relayState.textContent = `relay: ${state.relay?.state || "unknown"}`;
  tmuxState.textContent = `tmux: ${state.tmux?.state || "unknown"}`;
  workspaceRelay.textContent = relayState.textContent;
  workspaceTmux.textContent = tmuxState.textContent;
  tmuxCommand.textContent = state.tmux?.attach_command || "tmux attach -t triagent";

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
      <div class="engine__gauge">
        <div class="engine__beam"></div>
      </div>
      <p class="engine__note">${stateNotes[state] || ""}</p>
      <div class="engine__meta">
        <span>mirror: ${(payload.mirror_view || payload.mirror_mode || "log").toUpperCase()}</span>
        <span>${payload.session_id ? payload.session_id.slice(0, 8) : "pending"}</span>
      </div>
    `;

    const previous = previousStates.get(name);
    if (previous && previous !== state) {
      card.classList.add("engine--ignite");
    }
    previousStates.set(name, state);
    engineCards.appendChild(card);

    const status = document.createElement("div");
    status.className = `status status--${state}`;
    status.innerHTML = `
      <div>
        <p class="status__name">${name}</p>
        <p class="status__detail">${payload.pane_target || "no pane"}</p>
      </div>
      <div class="status__meta">
        <span>${payload.mirror_view || payload.mirror_mode || "log"}</span>
        <span>${stateLabels[state] || state}</span>
      </div>
    `;
    statusGrid.appendChild(status);
  }
}

function renderTranscript(entries) {
  transcript.innerHTML = "";
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
