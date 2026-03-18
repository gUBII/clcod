#!/usr/bin/env bash
# start.sh - Launch the clcod supervisor, UI, and tmux debug mirrors.

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
ui = config["ui"]

print(config["tmux"]["session"])
print(str(workspace["log_path"]))
print(str(workspace["lock_path"]))
print(str(workspace["relay_log_path"]))
print(str(workspace["pid_path"]))
print(build_ui_url := f"http://{ui['host']}:{ui['port']}")
print("1" if ui["open_browser"] else "0")
PY
)

SESSION="${SETTINGS[0]:-triagent}"
LOG_PATH="${SETTINGS[1]:-$DIR/clcodgemmix.txt}"
LOCK_PATH="${SETTINGS[2]:-$DIR/speaker.lock}"
SUPERVISOR_LOG="${SETTINGS[3]:-$DIR/.clcod-runtime/relay.log}"
PID_FILE="${SETTINGS[4]:-$DIR/.clcod-runtime/supervisor.pid}"
UI_URL="${SETTINGS[5]:-http://127.0.0.1:4173}"
OPEN_BROWSER="${SETTINGS[6]:-1}"

chmod +x "$DIR/supervisor.py" "$DIR/relay.py" "$DIR/join.py" "$DIR/stop.sh" "$DIR/watch-log.sh" "$DIR/healthcheck.sh"
mkdir -p "$(dirname "$LOG_PATH")" "$(dirname "$SUPERVISOR_LOG")" "$(dirname "$PID_FILE")" "$(dirname "$LOCK_PATH")"
touch "$LOG_PATH" "$SUPERVISOR_LOG"

CONFIG="$CONFIG" bash "$DIR/stop.sh" >/dev/null 2>&1 || true
rm -f "$LOCK_PATH"

nohup python3 "$DIR/supervisor.py" --config "$CONFIG" >>"$SUPERVISOR_LOG" 2>&1 &
LAUNCH_PID="$!"

SUPERVISOR_PID=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if [[ -f "$PID_FILE" ]]; then
    SUPERVISOR_PID="$(<"$PID_FILE")"
    if [[ "$SUPERVISOR_PID" =~ ^[0-9]+$ ]] && kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
      break
    fi
  fi
  sleep 0.5
done

if [[ ! "$SUPERVISOR_PID" =~ ^[0-9]+$ ]] && [[ "$LAUNCH_PID" =~ ^[0-9]+$ ]] && kill -0 "$LAUNCH_PID" 2>/dev/null; then
  SUPERVISOR_PID="$LAUNCH_PID"
fi

if [[ ! "$SUPERVISOR_PID" =~ ^[0-9]+$ ]] || ! kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
  echo "[start] supervisor failed to start; inspect $SUPERVISOR_LOG" >&2
  exit 1
fi

if [[ "$OPEN_BROWSER" == "1" ]]; then
  if command -v open >/dev/null 2>&1; then
    open "$UI_URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$UI_URL" >/dev/null 2>&1 || true
  fi
fi

echo ""
echo "  clcod is live."
echo ""
echo "  UI:       $UI_URL"
echo "  Attach:   tmux attach -t $SESSION"
echo "  Chat:     python3 $DIR/join.py --config $CONFIG --name ${USER:-Observer}"
echo "  Stop:     CONFIG=$CONFIG bash $DIR/stop.sh"
echo "  Health:   CONFIG=$CONFIG bash $DIR/healthcheck.sh"
echo "  Runtime:  $SUPERVISOR_LOG"
echo ""
