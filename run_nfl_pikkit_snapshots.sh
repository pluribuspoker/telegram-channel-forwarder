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

# Flap damping (deploy/hc_flap.py): a job that fails on and off stays DOWN
# until an hour passes with no new failure, instead of paging every run.
flap() { python3 "$APP_DIR/deploy/hc_flap.py" "$1" nfl-pikkit-snapshots; }

cd "$APP_DIR"
KILLED=$(flap start)
[ -n "$KILLED" ] && ping_hc "/fail" "$KILLED"
ping_hc "/start"

$PYTHON scripts/fetch_nfl_pikkit.py --write 2>&1 | tee "$LOGFILE"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    HELD=$(flap ok)
    if [ -n "$HELD" ]; then
        ping_hc "/log" "$HELD"$'\n\n'"$(tail -20 "$LOGFILE")"
    else
        ping_hc "" "$(tail -20 "$LOGFILE")"
    fi
else
    flap fail
    ping_hc "/fail" "$(tail -50 "$LOGFILE")"
fi
exit "$STATUS"
