#!/bin/bash
# Nightly offline test sweep — invoked by test-sweep.timer at 02:40 ET.
# scripts/run_tests.py runs every offline scripts/test_*.py in a scratch clone
# (one at a time — ~1 GB RAM), records per-test timings to
# logs/test_runs.jsonl, and DMs the operator via the watchdog bot only on a
# slow suite/test or the Sunday weekly CI-time line; failing tests go to the
# repair agent below, whose DM carries the result.
# Failing TESTS are content, not a job failure: the healthcheck pings success
# whenever the sweep itself completed (exit 0 or 1), so hc-repair never spends
# an agent on a red test. Only a crashed runner (exit ≥ 2) pings /fail.

# Without pipefail, `python ... | tee` returns tee's status (always 0).
set -o pipefail

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"
LOGFILE="/tmp/test_sweep_last_run.log"

ping_hc() {
    local suffix="${1:-}"
    local body="${2:-}"
    [ -n "$TEST_SWEEP_HEALTHCHECK_URL" ] || return 0
    if [ -n "$body" ]; then
        curl -fsS --retry 3 --data-raw "$body" "${TEST_SWEEP_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    else
        curl -fsS --retry 3 "${TEST_SWEEP_HEALTHCHECK_URL}${suffix}" > /dev/null 2>&1 || true
    fi
}

cd "$APP_DIR"
ping_hc "/start"
$PYTHON scripts/run_tests.py --trigger nightly --notify --repair-handoff 2>&1 | tee "$LOGFILE"
STATUS=$?

# Red tests → ONE repair agent fixes them, verifies with its own sandboxed
# run, pushes only if green, and DMs the result (scripts/nightly_test_repair.py;
# kill switch TEST_REPAIR_DISABLED=1 → a plain failure DM instead).
if [ "$STATUS" -eq 1 ]; then
    $PYTHON scripts/nightly_test_repair.py 2>&1 | tee -a "$LOGFILE"
fi

if [ "$STATUS" -le 1 ]; then
    ping_hc "" "$(tail -20 "$LOGFILE")"
    exit 0
fi
ping_hc "/fail" "$(tail -50 "$LOGFILE")"
exit "$STATUS"
