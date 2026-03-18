#!/usr/bin/env bash
# healthcheck.sh - Report relay, lock, transcript, and tmux session health.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1]).expanduser()
if not config_path.is_absolute():
    config_path = (Path.cwd() / config_path).resolve()
base = config_path.parent
data = {}
if config_path.exists():
    data = json.loads(config_path.read_text(encoding="utf-8"))

workspace = data.get("workspace", {})
locks = data.get("locks", {})
tmux_cfg = data.get("tmux", {})

def resolve(raw, fallback):
    value = Path(str(raw or fallback)).expanduser()
    if not value.is_absolute():
        value = (base / value).resolve()
    return str(value)

print(str(tmux_cfg.get("session", "triagent")))
print(resolve(workspace.get("log_path"), "clcodgemmix.txt"))
print(resolve(workspace.get("lock_path"), "speaker.lock"))
print(resolve(workspace.get("pid_path"), ".clcod-runtime/relay.pid"))
print(str(int(locks.get("ttl", 90))))
PY
)

SESSION="${SETTINGS[0]:-triagent}"
LOG_PATH="${SETTINGS[1]:-$DIR/clcodgemmix.txt}"
LOCK_PATH="${SETTINGS[2]:-$DIR/speaker.lock}"
PID_FILE="${SETTINGS[3]:-$DIR/.clcod-runtime/relay.pid}"
TTL="${SETTINGS[4]:-90}"
STATUS=0

if [[ -f "$PID_FILE" ]]; then
  RELAY_PID="$(<"$PID_FILE")"
  if [[ "$RELAY_PID" =~ ^[0-9]+$ ]] && kill -0 "$RELAY_PID" 2>/dev/null; then
    echo "[HEALTH] relay pid alive: $RELAY_PID"
  else
    echo "[HEALTH] WARNING: relay pid file exists but process is not running"
    STATUS=1
  fi
else
  echo "[HEALTH] WARNING: relay pid file not found"
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

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "[HEALTH] tmux session exists: $SESSION"
else
  echo "[HEALTH] WARNING: tmux session missing: $SESSION"
  STATUS=1
fi

exit "$STATUS"
