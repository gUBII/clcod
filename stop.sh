#!/usr/bin/env bash
# stop.sh — Kill everything

pkill -f "relay.py" 2>/dev/null
pkill -f "join.py" 2>/dev/null
tmux kill-session -t triagent 2>/dev/null
rm -f "$(dirname "$0")/speaker.lock"
echo "stopped"
