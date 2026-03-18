#!/usr/bin/env bash
# watch-log.sh - Tail the configured shared transcript.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$DIR/config.json"
LOG_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG="$2"
      shift 2
      ;;
    --log)
      LOG_OVERRIDE="$2"
      shift 2
      ;;
    *)
      echo "usage: $0 [--config path] [--log path]" >&2
      exit 1
      ;;
  esac
done

if [[ -n "$LOG_OVERRIDE" ]]; then
  LOG_PATH="$LOG_OVERRIDE"
else
  LOG_PATH="$(
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
value = Path(str(workspace.get("log_path", "clcodgemmix.txt"))).expanduser()
if not value.is_absolute():
    value = (base / value).resolve()
print(value)
PY
  )"
fi

mkdir -p "$(dirname "$LOG_PATH")"
touch "$LOG_PATH"

clear
printf 'Watching shared log: %s\n---\n' "$LOG_PATH"
tail -n 30 -F "$LOG_PATH"
