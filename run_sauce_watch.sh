#!/bin/bash
# Sauce open-bets watcher — invoked by sauce-watch.timer (~every 20 min + jitter)
# One anonymous GET against the published sheet; the full pipeline (Claude parse,
# grade, image, Telegram send) runs ONLY when the sheet has picks the DB hasn't
# seen. Shares /tmp/sauce_daily.lock with run_sauce_daily.sh so a watch tick and
# the 6 AM cron can't interleave DB writes or double-send.

# Without pipefail, `python ... | tee` returns tee's status (always 0), so a
# crashed run would read as success.
set -o pipefail

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"
LOGFILE="/tmp/sauce_watch_last_run.log"
LOCKFILE="/tmp/sauce_daily.lock"

ping_hc() {
    local suffix="${1:-}"
    local body="${2:-}"
    [ -n "$SAUCE_WATCH_HEALTHCHECK_URL" ] || return 0
    if [ -n "$body" ]; then
        curl -fsS --retry 3 --data-raw "$body" "${SAUCE_WATCH_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    else
        curl -fsS --retry 3 "${SAUCE_WATCH_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    fi
}

cd "$APP_DIR"
source .env
[ -f .env.local ] && source .env.local

exec 9>"$LOCKFILE"
if ! flock -w 300 9; then
    # Daily run (or a previous tick) still holds the lock — skip, next tick is ≤25 min away
    echo "Could not take $LOCKFILE within 300s; skipping this tick."
    exit 0
fi

ping_hc "/start"
if $PYTHON scripts/sauce_daily.py --channel -1003977774560 --only-if-new 2>&1 | tee "$LOGFILE"; then
    ping_hc "" "$(tail -20 "$LOGFILE")"
else
    ping_hc "/fail" "$(tail -50 "$LOGFILE")"
    exit 1
fi
