#!/bin/bash
# Wrap a command we don't own (cron jobs) with healthchecks.io pings:
#   hc_run.sh <ENV_KEY> <command> [args...]
# Reads <ENV_KEY> from the app's .env.local (then .env), pings /start, runs
# the command, then pings /<exit status> (0 = success, else fail). Exits with
# the command's status. Key unset = runs the command with no pings.
# Example (root crontab): hc_run.sh ROOT_BACKUP_HEALTHCHECK_URL /root/backup.sh
set -o pipefail
APP_DIR="/home/forwarder/app"
KEY="$1"; shift

URL=""
for f in "$APP_DIR/.env.local" "$APP_DIR/.env"; do
    [ -n "$URL" ] && break
    URL=$(grep -E "^${KEY}=" "$f" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "'\"")
done

ping_hc() {
    [ -n "$URL" ] || return 0
    curl -fsS -m 10 --retry 3 --data-raw "${2:-}" "${URL}$1" > /dev/null 2>&1 || true
}

ping_hc "/start"
OUT=$(mktemp)
"$@" 2>&1 | tee "$OUT"
STATUS=$?
ping_hc "/$STATUS" "$(tail -30 "$OUT")"
rm -f "$OUT"
exit "$STATUS"
