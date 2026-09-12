#!/bin/bash
# NFL Pikkit snapshot collector — invoked every 15 minutes.

set -o pipefail

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"
LOGFILE="/tmp/nfl_pikkit_snapshots_last_run.log"

ping_hc() {
    local suffix="${1:-}"
    local body="${2:-}"
    [ -n "$PIKKIT_SNAPSHOT_HEALTHCHECK_URL" ] || return 0
    if [ -n "$body" ]; then
        curl -fsS --retry 3 --data-raw "$body" "${PIKKIT_SNAPSHOT_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    else
        curl -fsS --retry 3 "${PIKKIT_SNAPSHOT_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    fi
}

cd "$APP_DIR"
ping_hc "/start"

$PYTHON scripts/fetch_nfl_pikkit.py --write 2>&1 | tee "$LOGFILE"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    ping_hc "" "$(tail -20 "$LOGFILE")"
else
    ping_hc "/fail" "$(tail -50 "$LOGFILE")"
fi
exit "$STATUS"
