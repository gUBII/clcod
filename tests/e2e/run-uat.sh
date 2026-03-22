#!/usr/bin/env bash
# UAT runner for Farhan's Man Cave
# Usage: ./run-uat.sh [--headed] [--spec <file>]
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

HEADED=""
SPEC=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --headed) HEADED="--headed"; shift ;;
    --spec)   SPEC="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Check server
if ! curl -s http://127.0.0.1:4173 -o /dev/null -w "%{http_code}" 2>/dev/null | grep -q "200"; then
  echo "❌  Server not responding at http://127.0.0.1:4173"
  echo "   Start it with: .venv/bin/python supervisor.py"
  exit 1
fi

echo "✅  Server is up — running UAT..."

cd "$SCRIPT_DIR"

if [[ -n "$SPEC" ]]; then
  npx playwright test --config=playwright.config.js $HEADED "specs/$SPEC"
else
  npx playwright test --config=playwright.config.js $HEADED
fi

echo ""
echo "📊  Report: npx playwright show-report ../../playwright-report"
