#!/bin/bash
# Pick grader — invoked by systemd timer every 5 minutes
# Signals healthchecks.io on start / success / failure
# Retries once on failure (next scheduled run is only 5 min away anyway)

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

ping_hc() {
    local suffix="${1:-}"
    [ -n "$TRACKER_HEALTHCHECK_URL" ] || return 0
    curl -fsS --retry 3 "${TRACKER_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
}

cd "$APP_DIR"

TRACKER_DAYS="${TRACKER_DAYS:-1}"

ping_hc "/start"
log "Starting pick grader (days=$TRACKER_DAYS)"
SUCCESS=0

for attempt in 1 2; do
    log "Attempt $attempt/2..."
    if $PYTHON tracker.py --live --days "$TRACKER_DAYS"; then
        SUCCESS=1
        break
    fi
    log "Attempt $attempt failed"
    [ "$attempt" -lt 2 ] && sleep 60
done

if [ "$SUCCESS" -eq 1 ]; then
    log "Completed successfully"
    ping_hc
else
    log "Both attempts failed"
    ping_hc "/fail"
    exit 1
fi
