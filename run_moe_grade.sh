#!/bin/bash
# MOE opinion grader — invoked by moe-grade.timer once a day at 05:23 ET.
# Signals healthchecks.io on start / success / failure (with log output).
# Deterministic: scripts/moe_grade.py makes no Claude call and opens no
# Telethon session — four Sheets reads, two ESPN scoreboard fetches, one
# append when there is something new. The ledger dedupes on opinion id, so a
# re-run is free. SINGLE attempt: the next timer slot is tomorrow, and a
# late-graded game costs nothing but time.

# Without pipefail, `python ... | tee` returns tee's status (always 0), so a
# crashed grader would be reported as success — no /fail ping.
set -o pipefail

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"
LOGFILE="/tmp/moe_grade_last_run.log"

log() { echo "$*"; }

ping_hc() {
    local suffix="${1:-}"
    local body="${2:-}"
    [ -n "$MOE_GRADE_HEALTHCHECK_URL" ] || return 0
    if [ -n "$body" ]; then
        curl -fsS --retry 3 --data-raw "$body" "${MOE_GRADE_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    else
        curl -fsS --retry 3 "${MOE_GRADE_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    fi
}

cd "$APP_DIR"

ping_hc "/start"
log "Starting MOE opinion grader"

$PYTHON scripts/moe_grade.py --write --notify 2>&1 | tee "$LOGFILE"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    log "Completed successfully"
    ping_hc "" "$(tail -20 "$LOGFILE")"
else
    log "Grader failed (exit $STATUS)"
    ping_hc "/fail" "$(tail -50 "$LOGFILE")"
fi
exit "$STATUS"
