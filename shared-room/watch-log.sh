#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_PATH="${LOG_PATH:-$SCRIPT_DIR/../clcodgemmix.txt}"

mkdir -p "$(dirname "$LOG_PATH")"
touch "$LOG_PATH"

clear
printf 'Watching shared log: %s\n---\n' "$LOG_PATH"
tail -n 20 -F "$LOG_PATH"
