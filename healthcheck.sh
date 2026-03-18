#!/usr/bin/env bash
# healthcheck.sh - Report supervisor, runtime state, transcript, and tmux health.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

CONFIG="${CONFIG:-$DIR/config.json}"
REPAIR=0

if [[ "${1:-}" == "--repair" ]]; then
  REPAIR=1
fi

SETTINGS=()
while IFS= read -r line; do
  SETTINGS+=("$line")
done < <(
  python3 - "$CONFIG" <<'PY'
import sys

import relay

config = relay.load_config(sys.argv[1])
workspace = config["workspace"]

print(config["tmux"]["session"])
print(str(workspace["log_path"]))
print(str(workspace["lock_path"]))
print(str(workspace["pid_path"]))
print(str(workspace["state_path"]))
print(str(int(config["locks"]["ttl"])))
PY
)

SESSION="${SETTINGS[0]:-triagent}"
LOG_PATH="${SETTINGS[1]:-$DIR/clcodgemmix.txt}"
LOCK_PATH="${SETTINGS[2]:-$DIR/speaker.lock}"
PID_FILE="${SETTINGS[3]:-$DIR/.clcod-runtime/supervisor.pid}"
STATE_PATH="${SETTINGS[4]:-$DIR/.clcod-runtime/state.json}"
TTL="${SETTINGS[5]:-90}"
STATUS=0

if [[ -f "$PID_FILE" ]]; then
  SUPERVISOR_PID="$(<"$PID_FILE")"
  if [[ "$SUPERVISOR_PID" =~ ^[0-9]+$ ]] && kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
    echo "[HEALTH] supervisor pid alive: $SUPERVISOR_PID"
  else
    echo "[HEALTH] WARNING: supervisor pid file exists but process is not running"
    STATUS=1
  fi
else
  echo "[HEALTH] WARNING: supervisor pid file not found"
  STATUS=1
fi

if [[ -f "$LOCK_PATH" ]]; then
  LOCK_AGE="$(python3 - "$LOCK_PATH" <<'PY'
import sys
import time
from pathlib import Path

lock_path = Path(sys.argv[1])
print(int(time.time() - lock_path.stat().st_mtime))
PY
)"
  echo "[HEALTH] lock age: ${LOCK_AGE}s"
  if (( LOCK_AGE > TTL )); then
    echo "[HEALTH] WARNING: stale lock detected (> ${TTL}s)"
    STATUS=1
    if (( REPAIR == 1 )); then
      rm -f "$LOCK_PATH"
      echo "[HEALTH] repaired stale lock"
    fi
  fi
else
  echo "[HEALTH] lock file not present"
fi

if [[ -f "$LOG_PATH" ]]; then
  echo "[HEALTH] transcript present: $LOG_PATH"
else
  echo "[HEALTH] WARNING: transcript missing: $LOG_PATH"
  STATUS=1
fi

if [[ -f "$STATE_PATH" ]]; then
  STATE_AGE="$(python3 - "$STATE_PATH" <<'PY'
import sys
import time
from pathlib import Path

state_path = Path(sys.argv[1])
print(int(time.time() - state_path.stat().st_mtime))
PY
)"
  echo "[HEALTH] state age: ${STATE_AGE}s"
  if (( STATE_AGE > 10 )); then
    echo "[HEALTH] WARNING: runtime state is stale"
    STATUS=1
  fi
  python3 - "$STATE_PATH" <<'PY'
import json
import sys

state = json.loads(open(sys.argv[1], "r", encoding="utf-8").read())
print(f"[HEALTH] app phase: {state['app']['phase']}")
print(f"[HEALTH] relay state: {state['relay']['state']}")
for name, payload in state["agents"].items():
    print(f"[HEALTH] {name} state: {payload['state']} mirror={payload['mirror_view']} pane={payload['pane_target']}")
PY
else
  echo "[HEALTH] WARNING: runtime state missing: $STATE_PATH"
  STATUS=1
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "[HEALTH] tmux session exists: $SESSION"
else
  echo "[HEALTH] WARNING: tmux session missing: $SESSION"
  STATUS=1
fi

exit "$STATUS"
