#!/bin/bash
# Trent pick monitor — invoked by systemd timer every 5 minutes
# Signals healthchecks.io on start / success / failure (with log output)
# Retries once on failure (next scheduled run is only 5 min away anyway)

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"
LOGFILE="/tmp/trent_monitor_last_run.log"

log() { echo "$*"; }

ping_hc() {
    local suffix="${1:-}"
    local body="${2:-}"
    [ -n "$TRENT_HEALTHCHECK_URL" ] || return 0
    if [ -n "$body" ]; then
        curl -fsS --retry 3 --data-raw "$body" "${TRENT_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    else
        curl -fsS --retry 3 "${TRENT_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    fi
}

cd "$APP_DIR"

ping_hc "/start"
log "Starting Trent monitor"
SUCCESS=0

for attempt in 1 2; do
    [ "$attempt" -gt 1 ] && log "Retry (attempt 2/2)..."
    if $PYTHON scripts/trent_monitor.py 2>&1 | tee "$LOGFILE"; then
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
