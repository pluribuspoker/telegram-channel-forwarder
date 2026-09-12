#!/bin/bash
# Launcher for claude-channels.service.
# Creates a tmux session (Claude needs a TTY) and monitors it with a
# systemd watchdog so crashes trigger an automatic restart.
#
# The model and effort are NOT hardcoded here: they come from $STATE_FILE,
# which the watchdog bot's /model and /effort commands write. A hardcoded
# --model cost a 14-hour outage on 2026-09-11 — the session answered every
# Telegram message with "You've reached your Fable limit" and the only way out
# was an SSH shell with this file open in an editor. The bot is the escape
# hatch precisely because it works from a phone.
#
# STATE_FILE lives OUTSIDE the repo on purpose: a `git pull` or a syncenv must
# never silently move the session back onto a model the account can't bill.

set -euo pipefail

SESSION="claude"
STATE_FILE="${CLAUDE_CHANNELS_STATE:-$HOME/.claude-channels.env}"
DEFAULT_MODEL="claude-opus-5"
DEFAULT_EFFORT="max"

# Read one KEY=value out of the state file. Deliberately NOT `source`: a file
# the bot half-wrote must fall back to the defaults, never execute and never
# take the launcher down with it.
state_value() {
    [ -f "$STATE_FILE" ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$STATE_FILE" \
        | tail -1 | tr -d "\"' " || true
}

MODEL="$(state_value CLAUDE_CHANNELS_MODEL)"
EFFORT="$(state_value CLAUDE_CHANNELS_EFFORT)"

# Both values land inside the tmux command line, so they are allowlisted, not
# trusted. Anything unexpected falls back rather than failing closed — being on
# the wrong model beats not starting at all.
[[ "$MODEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*(\[1m\])?$ ]] || MODEL="$DEFAULT_MODEL"
case "$EFFORT" in
    low|medium|high|xhigh|max) ;;
    *) EFFORT="$DEFAULT_EFFORT" ;;
esac

# The one place model resolution lives — the watchdog bot reports "what a
# restart would use" by calling this, so the answer can't drift from the truth.
if [ "${1:-}" = "--print-model" ]; then
    echo "model=$MODEL"
    echo "effort=$EFFORT"
    if [ -f "$STATE_FILE" ]; then echo "source=$STATE_FILE"; else echo "source=defaults"; fi
    exit 0
fi

CLAUDE_CMD="claude --channels plugin:telegram@claude-plugins-official --dangerously-skip-permissions --model $MODEL --effort $EFFORT"
echo "claude-channels: starting model=$MODEL effort=$EFFORT (from ${STATE_FILE})" >&2

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

    # Check bun (Telegram plugin) is running
    if ! pgrep -f "bun server.ts" >/dev/null 2>&1; then
        echo "claude-channels: bun/telegram plugin gone, exiting for restart" >&2
        exit 1
    fi

    systemd-notify WATCHDOG=1
done
