#!/bin/bash
# God Expert judge runner — invoked by god-judge.timer at :12 and :42
# Signals healthchecks.io on start / success / failure (with log output)
# SINGLE attempt, no retry: every judge call bills the Claude subscription,
# so a retry would re-bill. The next timer slot is 30 minutes away and the
# runner dedupes by committee key, so a failed pass costs nothing but time.

# Without pipefail, `python ... | tee` returns tee's status (always 0), so a
# crashed runner would be reported as success — no /fail ping.
set -o pipefail

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"
LOGFILE="/tmp/god_judge_last_run.log"

log() { echo "$*"; }

ping_hc() {
    local suffix="${1:-}"
    local body="${2:-}"
    [ -n "$GOD_JUDGE_HEALTHCHECK_URL" ] || return 0
    if [ -n "$body" ]; then
        curl -fsS --retry 3 --data-raw "$body" "${GOD_JUDGE_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    else
        curl -fsS --retry 3 "${GOD_JUDGE_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    fi
}

# Flap damping (deploy/hc_flap.py): a job that fails on and off stays DOWN
# until an hour passes with no new failure, instead of paging every run.
flap() { python3 "$APP_DIR/deploy/hc_flap.py" "$1" god-judge; }

cd "$APP_DIR"

flap start
ping_hc "/start"
log "Starting God Expert judge runner"

$PYTHON scripts/god_judge_runner.py 2>&1 | tee "$LOGFILE"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    log "Completed successfully"
    HELD=$(flap ok)
    if [ -n "$HELD" ]; then
        ping_hc "/log" "$HELD"$'\n\n'"$(tail -20 "$LOGFILE")"
    else
        ping_hc "" "$(tail -20 "$LOGFILE")"
    fi
else
    log "Runner failed (exit $STATUS)"
    flap fail
    ping_hc "/fail" "$(tail -50 "$LOGFILE")"
fi
exit "$STATUS"
