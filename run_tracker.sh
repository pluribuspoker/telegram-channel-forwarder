#!/bin/bash
# Pick grader — invoked by systemd timer every 5 minutes
# Signals healthchecks.io on start / success / failure (with log output)
# Retries once on failure (next scheduled run is only 5 min away anyway)

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"
LOGFILE="/tmp/tracker_last_run.log"

log() { echo "[$(date '+%m/%d %I:%M:%S %p ET')] $*"; }

ping_hc() {
    local suffix="${1:-}"
    local body="${2:-}"
    [ -n "$TRACKER_HEALTHCHECK_URL" ] || return 0
    if [ -n "$body" ]; then
        curl -fsS --retry 3 --data-raw "$body" "${TRACKER_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    else
        curl -fsS --retry 3 "${TRACKER_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    fi
}

cd "$APP_DIR"

TRACKER_DAYS="${TRACKER_DAYS:-1}"

ping_hc "/start"
log "Starting pick grader (days=$TRACKER_DAYS)"
SUCCESS=0

for attempt in 1 2; do
    [ "$attempt" -gt 1 ] && log "Retry (attempt 2/2)..."
    if $PYTHON tracker.py --live --days "$TRACKER_DAYS" 2>&1 | tee "$LOGFILE"; then
        SUCCESS=1
        break
    fi
    [ "$attempt" -lt 2 ] && sleep 60
done

if [ "$SUCCESS" -eq 1 ]; then
    log "Completed successfully"
    ping_hc "" "$(tail -20 "$LOGFILE")"
else
    log "Both attempts failed"
    ping_hc "/fail" "$(tail -50 "$LOGFILE")"
    exit 1
fi
