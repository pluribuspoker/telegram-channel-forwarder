#!/bin/bash
# Nightly pick grader — invoked by systemd timer at 3 AM ET
# Signals healthchecks.io on start / success / failure
# Retries up to 3 times on failure before giving up

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

ping_hc() {
    local suffix="${1:-}"
    [ -n "$TRACKER_HEALTHCHECK_URL" ] || return 0
    curl -fsS --retry 3 "${TRACKER_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
}

cd "$APP_DIR"

TRACKER_DAYS="${TRACKER_DAYS:-2}"

ping_hc "/start"
log "Starting nightly pick grader (days=$TRACKER_DAYS)"
SUCCESS=0

for attempt in 1 2 3; do
    log "Attempt $attempt/3..."
    if $PYTHON tracker.py --live --days "$TRACKER_DAYS"; then
        SUCCESS=1
        break
    fi
    log "Attempt $attempt failed"
    [ "$attempt" -lt 3 ] && sleep 300
done

if [ "$SUCCESS" -eq 1 ]; then
    log "Completed successfully"
    ping_hc
else
    log "All 3 attempts failed"
    ping_hc "/fail"
    exit 1
fi
