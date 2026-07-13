#!/bin/bash
# Launcher for claude-channels.service.
# Creates a tmux session (Claude needs a TTY) and monitors it with a
# systemd watchdog so crashes trigger an automatic restart.

set -euo pipefail

SESSION="claude"
CLAUDE_CMD="claude --channels plugin:telegram@claude-plugins-official --dangerously-skip-permissions --model opus[1m]"

# Kill orphan bun processes from a previous crash. The Telegram plugin's
# bot.pid cleanup only works on graceful shutdown — a SIGKILL leaves the
# old poller running and the new one gets 409 Conflict.
pkill -f "bun server.ts" 2>/dev/null || true
sleep 1

# Start claude in a detached tmux session
tmux new-session -d -s "$SESSION" "cd ~/app && $CLAUDE_CMD"

# Watchdog loop: confirm the tmux session + claude process are alive.
# Send WATCHDOG=1 to systemd each iteration so it knows we're healthy.
while true; do
    sleep 20

    # Check tmux session exists
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "claude-channels: tmux session gone, exiting for restart" >&2
        exit 1
    fi

    # Check claude process is running inside the session
    if ! pgrep -f "claude --channels" >/dev/null 2>&1; then
        echo "claude-channels: claude process gone, exiting for restart" >&2
        exit 1
    fi

    systemd-notify WATCHDOG=1
done
