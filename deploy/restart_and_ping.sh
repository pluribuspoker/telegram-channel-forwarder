#!/bin/bash
# Detached self-restart for the Claude Telegram session — launched by that
# session itself when the operator asks for a restart/reset in the Claude chat:
#
#   sudo -n systemd-run --collect --unit="claude-restart-$(date +%s)" \
#     /home/forwarder/app/deploy/restart_and_ping.sh <chat_id>
#
# systemd-run puts us in our own scope, so we survive the KillMode=control-group
# stop of claude-channels.service that kills the session and everything it
# spawned — an inline `systemctl restart` run by the session dies mid-call with
# no confirmation. Flow: settle pause (lets the session's goodbye reply flush) →
# restart the service → wait for tmux + claude + bun (the receive loop) → ping
# the chat through the plugin's own bot token. On timeout the ping says to fall
# back to the watchdog bot.
#
# --check: validate config + current liveness (no restart, no message sent).
set -uo pipefail

CHAT_ID="${1:-}"
CHECK=0
[ "$CHAT_ID" = "--check" ] && { CHECK=1; CHAT_ID="${2:-}"; }

APP_ENV="/home/forwarder/app/.env"
PLUGIN_ENV="/home/forwarder/.claude/channels/telegram/.env"

envval() {  # envval KEY FILE — value of KEY=... (first match, quotes/CR stripped)
  grep "^${1}=" "$2" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r"'
}

# Manual-run convenience: default to the operator's id from the app .env.
if [ -z "$CHAT_ID" ]; then
  CHAT_ID="$(envval WATCHDOG_USER_ID "$APP_ENV")"
fi
[ -n "$CHAT_ID" ] || { echo "no chat_id (arg or WATCHDOG_USER_ID in .env)" >&2; exit 1; }

BOT_TOKEN="$(envval TELEGRAM_BOT_TOKEN "$PLUGIN_ENV")"

ping() {
  [ -n "$BOT_TOKEN" ] || return 0
  curl -s --max-time 20 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=$1" >/dev/null 2>&1
}

stack_up() {  # tmux session + claude process + bun poller (the receive loop)
  runuser -u forwarder -- tmux has-session -t claude 2>/dev/null \
    && pgrep -f 'claude --channels' >/dev/null 2>&1 \
    && pgrep -f 'bun server.ts' >/dev/null 2>&1
}

if [ "$CHECK" = "1" ]; then
  fail=0
  echo "chat_id: set"
  if [ -n "$BOT_TOKEN" ] && curl -s --max-time 15 \
      "https://api.telegram.org/bot${BOT_TOKEN}/getMe" | grep -q '"ok":true'; then
    echo "bot token: ok"
  else
    echo "bot token: BAD"; fail=1
  fi
  if stack_up; then echo "stack: up"; else echo "stack: DOWN"; fail=1; fi
  exit "$fail"
fi

# Let the session finish its turn — its "restarting" reply is sent before we
# are launched; this is slack for the tool result and final output to land.
sleep 5

systemctl restart claude-channels.service

# Wait for the full stack. The SessionStart "▶️ Restarted" hook message fires
# before the receive loop is ready, so it alone doesn't mean messages get
# through; bun polling is the real signal. ~2 min budget.
up=0
for _ in $(seq 1 40); do
  sleep 3
  if stack_up; then up=1; break; fi
done

# A few more seconds for the poller to actually connect.
sleep 8

if [ "$up" = "1" ]; then
  ping "✅ Back online — fresh session, clean context. Wait for the 👀 on your next message before firing a real task."
else
  ping "⚠️ Restart fired but the new session didn't come up within ~2 min. Check the watchdog bot: /status /logs /restart."
fi
