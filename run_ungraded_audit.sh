#!/bin/bash
# Nightly ungraded-pick audit — invoked by ungraded-audit.timer at 04:05 ET.
# Signals healthchecks.io on start / success / failure when
# UNGRADED_AUDIT_HEALTHCHECK_URL is set (unset = silent no-op, same
# convention as every other runner).
# SINGLE attempt, no retry: every agent bills the Claude subscription and the
# runner itself caps picks, budget, and per-agent timeouts; a failed night is
# reported by DM and retried by tomorrow's timer.

# Without pipefail, `python ... | tee` returns tee's status (always 0), so a
# crashed runner would be reported as success — no /fail ping.
set -o pipefail

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"
LOGFILE="/tmp/ungraded_audit_last_run.log"

ping_hc() {
    local suffix="${1:-}"
    local body="${2:-}"
    [ -n "$UNGRADED_AUDIT_HEALTHCHECK_URL" ] || return 0
    if [ -n "$body" ]; then
        curl -fsS --retry 3 --data-raw "$body" "${UNGRADED_AUDIT_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    else
        curl -fsS --retry 3 "${UNGRADED_AUDIT_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    fi
}

cd "$APP_DIR"

ping_hc "/start"
echo "Starting nightly ungraded-pick audit"

$PYTHON scripts/ungraded_audit.py "$@" 2>&1 | tee "$LOGFILE"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
    echo "Completed successfully"
    ping_hc "" "$(tail -30 "$LOGFILE")"
else
    echo "Runner failed (exit $STATUS)"
    ping_hc "/fail" "$(tail -50 "$LOGFILE")"
fi
exit "$STATUS"
