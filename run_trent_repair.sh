#!/bin/bash
# Trent auto-repair — started on demand by run_trent_watcher.sh's failure
# branch (systemctl start --no-block trent-repair.service) or manually.
# All guards (kill switch, cooldown, attempt cap, flock) live in
# scripts/trent_repair.py; this wrapper only logs and pings healthchecks.
# Signals TRENT_REPAIR_HEALTHCHECK_URL on start / success / failure when set
# (unset = silent no-op, same convention as every other runner).

# Without pipefail, `python ... | tee` returns tee's status (always 0), so a
# crashed runner would be reported as success — no /fail ping.
set -o pipefail

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"
LOGFILE="/tmp/trent_repair_last_run.log"

ping_hc() {
    local suffix="${1:-}"
    local body="${2:-}"
    [ -n "$TRENT_REPAIR_HEALTHCHECK_URL" ] || return 0
    if [ -n "$body" ]; then
        curl -fsS --retry 3 --data-raw "$body" "${TRENT_REPAIR_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    else
        curl -fsS --retry 3 "${TRENT_REPAIR_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    fi
}

cd "$APP_DIR"

ping_hc "/start"
echo "Starting Trent auto-repair"

$PYTHON scripts/trent_repair.py "$@" 2>&1 | tee "$LOGFILE"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    echo "Completed"
    ping_hc "" "$(tail -30 "$LOGFILE")"
else
    echo "Repair failed (exit $STATUS)"
    ping_hc "/fail" "$(tail -50 "$LOGFILE")"
fi
exit "$STATUS"
