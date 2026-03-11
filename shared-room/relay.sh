#!/usr/bin/env bash
# relay.sh — routes new log messages to Codex and Gemini in parallel

LOG="/Users/moofasa/clcod/clcodgemmix.txt"
LAST_SIZE=$(wc -c < "$LOG")
TMPDIR_R=$(mktemp -d)

echo "[relay] watching $LOG ..."
trap 'rm -rf "$TMPDIR_R"' EXIT

while true; do
  sleep 1
  CURRENT_SIZE=$(wc -c < "$LOG")
  [[ "$CURRENT_SIZE" -le "$LAST_SIZE" ]] && continue

  NEW=$(tail -c "+$((LAST_SIZE + 1))" "$LOG")
  LAST_SIZE="$CURRENT_SIZE"

  SPEAKER=$(printf '%s' "$NEW" | grep -oE '^\[[A-Z]+\]' | tail -1 | tr -d '[]')
  [[ -z "$SPEAKER" ]] && continue
  [[ "$SPEAKER" == "CODEX" || "$SPEAKER" == "GEMINI" ]] && continue

  MSG=$(printf '%s' "$NEW" | sed -n "/^\[$SPEAKER\]/,\$p" | tail -n +2 | sed '/^[[:space:]]*$/d')
  [[ -z "$MSG" ]] && continue

  CONTEXT="You are one of three AI agents (Claude, Codex, Gemini) in a shared real-time terminal chat room. The repo is at /Users/moofasa/clcod/. You may read files in this repo when doing code review. Reply naturally and directly. When asked to review code, be specific. [$SPEAKER] just said:

$MSG"

  echo "[relay] $SPEAKER spoke → routing to Codex + Gemini in parallel..."

  CODEX_OUT="$TMPDIR_R/codex.txt"
  GEMINI_OUT="$TMPDIR_R/gemini.txt"

  # run both in background
  codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
    -C /Users/moofasa/clcod "$CONTEXT" > "$CODEX_OUT" 2>/dev/null &
  CODEX_PID=$!

  gemini -p "$CONTEXT" > "$GEMINI_OUT" 2>/dev/null &
  GEMINI_PID=$!

  # wait up to 45s
  for i in $(seq 1 45); do
    sleep 1
    CODEX_DONE=0; GEMINI_DONE=0
    kill -0 "$CODEX_PID" 2>/dev/null || CODEX_DONE=1
    kill -0 "$GEMINI_PID" 2>/dev/null || GEMINI_DONE=1
    [[ "$CODEX_DONE" -eq 1 && "$GEMINI_DONE" -eq 1 ]] && break
  done

  kill "$CODEX_PID" "$GEMINI_PID" 2>/dev/null

  CODEX_REPLY=$(cat "$CODEX_OUT" 2>/dev/null | grep -vE '^(OpenAI|---|workdir|model:|provider|approval|sandbox|reasoning|session|user$|mcp |thinking|\*\*|codex$|tokens)' | sed '/^[[:space:]]*$/d' | tail -10)
  GEMINI_REPLY=$(cat "$GEMINI_OUT" 2>/dev/null | grep -v 'Loaded cached' | sed '/^$/d' | head -15)

  if [[ -n "$CODEX_REPLY" ]]; then
    printf '\n[CODEX]\n%s\n' "$CODEX_REPLY" >> "$LOG"
    echo "[relay] Codex replied"
  else
    echo "[relay] Codex: no reply"
  fi

  LAST_SIZE=$(wc -c < "$LOG")

  if [[ -n "$GEMINI_REPLY" ]]; then
    printf '\n[GEMINI]\n%s\n' "$GEMINI_REPLY" >> "$LOG"
    echo "[relay] Gemini replied"
  else
    echo "[relay] Gemini: no reply"
  fi

  LAST_SIZE=$(wc -c < "$LOG")
done
