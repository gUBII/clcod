#!/usr/bin/env bash
# stop.sh - Stop the relay and tmux room using config-derived paths.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${CONFIG:-$DIR/config.json}"

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
tmux_cfg = data.get("tmux", {})

def resolve(raw, fallback):
    value = Path(str(raw or fallback)).expanduser()
    if not value.is_absolute():
        value = (base / value).resolve()
    return str(value)

print(str(tmux_cfg.get("session", "triagent")))
print(resolve(workspace.get("lock_path"), "speaker.lock"))
print(resolve(workspace.get("pid_path"), ".clcod-runtime/relay.pid"))
PY
)

SESSION="${SETTINGS[0]:-triagent}"
LOCK_PATH="${SETTINGS[1]:-$DIR/speaker.lock}"
PID_FILE="${SETTINGS[2]:-$DIR/.clcod-runtime/relay.pid}"

if [[ -f "$PID_FILE" ]]; then
  RELAY_PID="$(<"$PID_FILE")"
  if [[ "$RELAY_PID" =~ ^[0-9]+$ ]] && kill -0 "$RELAY_PID" 2>/dev/null; then
    kill "$RELAY_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      if ! kill -0 "$RELAY_PID" 2>/dev/null; then
        break
      fi
      sleep 0.2
    done
  fi
  rm -f "$PID_FILE"
fi

tmux kill-session -t "$SESSION" 2>/dev/null || true
rm -f "$LOCK_PATH"

echo "stopped"
