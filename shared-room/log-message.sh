#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_PATH="${LOG_PATH:-$SCRIPT_DIR/../clcodgemmix.txt}"

mkdir -p "$(dirname "$LOG_PATH")"
touch "$LOG_PATH"

if [[ $# -lt 2 ]]; then
  cat <<'USAGE' >&2
Usage: $0 <speaker> <message...>
# Appends a timestamped line to the shared log.
USAGE
  exit 1
fi

speaker="$1"
shift
message="$*"

echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') [$speaker] $message" >> "$LOG_PATH"
LC_NO_COLOR=1 tail -n 5 "$LOG_PATH" >&2
