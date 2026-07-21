#!/bin/bash
# Trent pick watcher — invoked by systemd timer every 15 minutes
# Signals healthchecks.io on start / success / failure (with log output)
# Retries once on failure (next scheduled run is only 15 min away anyway)

# Without pipefail, `python ... | tee` returns tee's status (always 0), so a
# non-zero exit from the watcher was reported as success — no retry, no /fail ping.
set -o pipefail

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"
LOGFILE="/tmp/trent_watcher_last_run.log"

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
log "Starting Trent watcher"
SUCCESS=0

for attempt in 1 2; do
    [ "$attempt" -gt 1 ] && log "Retry (attempt 2/2)..."
    if $PYTHON scripts/trent_watcher.py 2>&1 | tee "$LOGFILE"; then
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
