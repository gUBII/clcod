#!/usr/bin/env bash
# start.sh — Launch the clcodgemmix shared chat room
#
# Creates a tmux session with:
#   Top:          Live chat log (tail -f)
#   Bottom-left:  Codex CLI (interactive)
#   Bottom-right: Gemini CLI (interactive)
#
# The relay runs in the background, routing messages to Codex & Gemini.
# Claude is handled by your active Claude Code session.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/clcodgemmix.txt"
SESSION="triagent"

# ── clean up ──────────────────────────────────────────────────────────────────
tmux kill-session -t "$SESSION" 2>/dev/null || true
pkill -f "relay.py" 2>/dev/null || true
sleep 0.5

# ── ensure log exists ─────────────────────────────────────────────────────────
touch "$LOG"

# ── start relay in background ─────────────────────────────────────────────────
python3 "$DIR/relay.py" --log "$LOG" > /tmp/relay.log 2>&1 &
RELAY_PID=$!
echo "[start] relay running (PID $RELAY_PID) — logs at /tmp/relay.log"

# ── build tmux session ────────────────────────────────────────────────────────
tmux new-session -d -s "$SESSION" -x 200 -y 50

# Pane 0: live log
tmux send-keys -t "$SESSION:0.0" "tail -f $LOG" Enter

# Split bottom row
tmux split-window -v -t "$SESSION:0.0" -p 35

# Pane 1: Codex CLI
tmux send-keys -t "$SESSION:0.1" "codex" Enter

# Pane 2: Gemini CLI (split pane 1 horizontally)
tmux split-window -h -t "$SESSION:0.1"
tmux send-keys -t "$SESSION:0.2" "gemini" Enter

# Pane 3: Claude CLI (split pane 2 horizontally)
tmux split-window -h -t "$SESSION:0.2"
tmux send-keys -t "$SESSION:0.3" "claude" Enter

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  clcodgemmix is live!"
echo ""
echo "  Attach:   tmux attach -t $SESSION"
echo "  Chat:     python3 $DIR/join.py --name Farhan"
echo "  Stop:     tmux kill-session -t $SESSION && pkill -f relay.py"
echo ""
echo "  Layout:"
echo "  ┌──────────────────────────────────────┐"
echo "  │         Live Chat Log                │"
echo "  ├───────────┬───────────┬──────────────┤"
echo "  │   Codex   │   Gemini  │    Claude    │"
echo "  └───────────┴───────────┴──────────────┘"
echo ""
