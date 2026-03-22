#!/usr/bin/env bash
# stop.sh - Stop the clcod supervisor and tmux room.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

CONFIG="${CONFIG:-$DIR/config.json}"

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
print(str(workspace["lock_path"]))
print(str(workspace["pid_path"]))
PY
)

SESSION="${SETTINGS[0]:-triagent}"
LOCK_PATH="${SETTINGS[1]:-$DIR/speaker.lock}"
PID_FILE="${SETTINGS[2]:-$DIR/.clcod-runtime/supervisor.pid}"

stop_pid() {
  local target_pid="$1"
  if [[ "$target_pid" =~ ^[0-9]+$ ]] && kill -0 "$target_pid" 2>/dev/null; then
    kill "$target_pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if ! kill -0 "$target_pid" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done
    if kill -0 "$target_pid" 2>/dev/null; then
      kill -9 "$target_pid" 2>/dev/null || true
    fi
  fi
}

if [[ -f "$PID_FILE" ]]; then
  SUPERVISOR_PID="$(<"$PID_FILE")"
  stop_pid "$SUPERVISOR_PID"
  rm -f "$PID_FILE"
fi

while IFS= read -r extra_pid; do
  stop_pid "$extra_pid"
done < <(
  ps axww -o pid=,command= 2>/dev/null | awk -v dir="$DIR" '
    {
      line = tolower($0)
      root = tolower(dir)
    }
    index(line, root) && line ~ /supervisor\.py/ { print $1 }
  ' || true
)

tmux kill-session -t "$SESSION" 2>/dev/null || true
rm -f "$LOCK_PATH"

echo "stopped"
