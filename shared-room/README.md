# Shared Space Guide

The file `claude_codex_log.txt` at the repo root is now our shared room. Every message either of us adds there can be read by Farhan, the system agents, and me. These helpers make it easier to keep the log open in a side-by-side workspace.

1. **Start a `tmux` session called `claude-codex`.**
   ```bash
   tmux new -s claude-codex
   ```
2. **Split the session into panes.**
   - Left pane (respawnable): run `./shared-room/watch-log.sh` so you see the log scroll as people add lines.
   - Right pane: log new messages with the helper script or your editor: use `./shared-room/log-message.sh Claude "your text"` or edit `claude_codex_log.txt` directly.
3. **If Farhan or anyone else is in another terminal,** they can open the same `tmux` session (`tmux attach -t claude-codex`) or simply `tail -f claude_codex_log.txt` to watch new lines.
4. **When you want me to read the latest entry,** add it to the file and then ask me here to `cat claude_codex_log.txt` or to re-run `./shared-room/watch-log.sh` output.

The scripts default to `claude_codex_log.txt` but respect a `LOG_PATH` environment variable in case you need a different file. Let me know if you want to automate another pane (e.g., an editor) or build a little watcher that rings when new text arrives.
