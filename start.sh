#!/usr/bin/env bash
# start.sh - Launch the clcod shared room and its tmux workspace.

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
agents = data.get("agents", [])

def resolve(raw, fallback):
    value = Path(str(raw or fallback)).expanduser()
    if not value.is_absolute():
        value = (base / value).resolve()
    return str(value)

print(str(tmux_cfg.get("session", "triagent")))
print(resolve(workspace.get("log_path"), "clcodgemmix.txt"))
print(resolve(workspace.get("lock_path"), "speaker.lock"))
print(resolve(workspace.get("relay_log_path"), ".clcod-runtime/relay.log"))
print(resolve(workspace.get("pid_path"), ".clcod-runtime/relay.pid"))
for agent in agents:
    if not agent.get("enabled", True):
        continue
    shell_cmd = str(agent.get("shell_cmd") or agent.get("cmd") or "").strip()
    if shell_cmd:
        print(shell_cmd)
PY
)

SESSION="${SETTINGS[0]:-triagent}"
LOG="${SETTINGS[1]:-$DIR/clcodgemmix.txt}"
LOCK_PATH="${SETTINGS[2]:-$DIR/speaker.lock}"
RELAY_LOG="${SETTINGS[3]:-$DIR/.clcod-runtime/relay.log}"
PID_FILE="${SETTINGS[4]:-$DIR/.clcod-runtime/relay.pid}"
AGENT_CMDS=("${SETTINGS[@]:5}")

chmod +x "$DIR/relay.py" "$DIR/join.py" "$DIR/stop.sh" "$DIR/watch-log.sh" "$DIR/healthcheck.sh"
mkdir -p "$(dirname "$LOG")" "$(dirname "$RELAY_LOG")" "$(dirname "$PID_FILE")" "$(dirname "$LOCK_PATH")"
touch "$LOG"

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(<"$PID_FILE")"
  if [[ "$OLD_PID" =~ ^[0-9]+$ ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null || true
    sleep 0.5
  fi
  rm -f "$PID_FILE"
fi

rm -f "$LOCK_PATH"
tmux kill-session -t "$SESSION" 2>/dev/null || true

python3 "$DIR/relay.py" --config "$CONFIG" >"$RELAY_LOG" 2>&1 &
RELAY_PID=$!
printf '%s\n' "$RELAY_PID" >"$PID_FILE"

if ! kill -0 "$RELAY_PID" 2>/dev/null; then
  echo "[start] relay failed to start; inspect $RELAY_LOG" >&2
  exit 1
fi

tmux new-session -d -s "$SESSION" -x 220 -y 55
tmux send-keys -t "$SESSION:0.0" "bash \"$DIR/watch-log.sh\" --config \"$CONFIG\"" Enter
tmux split-window -v -t "$SESSION:0.0" -p 35

if [[ ${#AGENT_CMDS[@]} -gt 0 ]]; then
  PANE_INDEX=1
  tmux send-keys -t "$SESSION:0.$PANE_INDEX" "${AGENT_CMDS[0]}" Enter
  for ((i = 1; i < ${#AGENT_CMDS[@]}; i++)); do
    tmux split-window -h -t "$SESSION:0.$PANE_INDEX"
    PANE_INDEX=$((PANE_INDEX + 1))
    tmux send-keys -t "$SESSION:0.$PANE_INDEX" "${AGENT_CMDS[$i]}" Enter
  done
fi

tmux select-layout -t "$SESSION:0" main-horizontal >/dev/null 2>&1 || true

echo ""
echo "  clcod is live."
echo ""
echo "  Attach:   tmux attach -t $SESSION"
echo "  Chat:     python3 $DIR/join.py --config $CONFIG --name ${USER:-Observer}"
echo "  Stop:     CONFIG=$CONFIG bash $DIR/stop.sh"
echo "  Health:   CONFIG=$CONFIG bash $DIR/healthcheck.sh"
echo "  Relay:    $RELAY_LOG"
echo ""
