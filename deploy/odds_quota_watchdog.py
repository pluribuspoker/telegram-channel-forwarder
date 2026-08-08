"""Odds API quota watchdog — DMs the operator before pricing goes dark.

Stays silent unless a threshold trips, then alerts through the watchdog bot
(same bot as mem_watchdog / claude_spend_watchdog).

Why this exists: on 2026-08-08 the monthly quota hit 20,000/20,000 and nothing
said so. There is no signal at all when it happens — `fetch_odds*` catches the
401 and returns `match_type="no_game"`, which is the SAME value a genuinely
missing event produces, so an outage is indistinguishable from a bad match by
anything downstream. Picks simply stop showing a price.

It is worse than a gap: the tracker stores the result and only fetches once
("if odds_by_pick is already in the cache, reuse it" — tracker.py), so every
pick posted during an outage keeps its empty odds FOREVER, including after the
quota resets. Each silent hour permanently strands that hour's picks.

  * 🛑 exhausted — remaining == 0. Picks are being stranded right now.
  * 📉 low       — remaining under ODDS_QUOTA_LOW_REMAINING, with the burn rate
    and the projected exhaustion date, so there's time to act before 🛑.

The check itself costs no quota: /v4/sports/ is a free endpoint
(`x-requests-last: 0`) and every response carries the usage headers.

Reuses WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID from .env. State (debounce +
usage history for the burn rate) lives in ~/.odds_quota_watchdog_state.json.

Usage:
    python deploy/odds_quota_watchdog.py            # normal (silent unless tripped)
    python deploy/odds_quota_watchdog.py --report   # print the numbers, send nothing
    python deploy/odds_quota_watchdog.py --force    # send even if under threshold / debounced
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
STATE = Path.home() / ".odds_quota_watchdog_state.json"

# A free endpoint: it returns the usage headers while spending nothing.
PROBE_URL = "https://api.the-odds-api.com/v4/sports/"

OUT_DEBOUNCE_SECS = 6 * 60 * 60    # at most one 🛑 per 6h (it's actively breaking pricing)
LOW_DEBOUNCE_SECS = 12 * 60 * 60   # at most one 📉 per 12h
HISTORY_KEEP_SECS = 8 * 24 * 60 * 60

# Units whose journals log the 401 body, to count damage already done.
DAMAGE_UNITS = "telegram-tracker,grade-daemon,telegram-forwarder"


def load_env() -> None:
    for name in (".env", ".env.local"):
        f = APP / name
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            m = line.strip()
            if m and not m.startswith("#") and "=" in m:
                k, v = m.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (OSError, ValueError):
            pass
    return {}


def save_state(s: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    os.replace(tmp, STATE)


def send(text: str) -> bool:
    token = os.environ.get("WATCHDOG_BOT_TOKEN", "")
    uid = os.environ.get("WATCHDOG_USER_ID", "")
    if not token or not uid:
        print("WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID not set", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({"chat_id": uid, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001 - never let a send failure kill the check
        print(f"send failed: {e}", file=sys.stderr)
        return False


def probe() -> tuple[int | None, int | None, int | None]:
    """(remaining, used, cost_of_this_call) from the Odds API usage headers.

    Returns (None, None, None) if the key is missing or the API is unreachable —
    a transport failure must not read as a quota problem.
    """
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        print("ODDS_API_KEY not set", file=sys.stderr)
        return None, None, None
    url = f"{PROBE_URL}?{urllib.parse.urlencode({'apiKey': key})}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            h = r.headers
    except urllib.error.HTTPError as e:
        # 401 here still carries the usage headers, and is exactly the
        # exhausted case we want to report rather than swallow.
        h = e.headers
    except Exception as e:  # noqa: BLE001
        print(f"probe failed: {e}", file=sys.stderr)
        return None, None, None

    def _int(name):
        v = (h.get(name) or "").strip()
        return int(v) if v.lstrip("-").isdigit() else None

    return _int("x-requests-remaining"), _int("x-requests-used"), _int("x-requests-last")


def damage_signals(hours: int = 1) -> tuple[int, bool]:
    """(quota failures in journald over the window, latest fast-path run failed too).

    Two sources because journald alone under-reports: the listener's and the
    Trent watcher's fast-path tracker runs are spawned with stdout/stderr
    DEVNULL'd and land in logs/tracker_quick.log instead. On 2026-08-08 that
    file held the only record of the failure while journald showed zero — a
    count of 0 printed beside a live outage would repeat the very "no signal"
    problem this watchdog exists to fix.

    The quick log has no per-line timestamps (only per-run headers), so it
    contributes a boolean about the most recent run rather than a windowed
    count. Better an honest flag than a precise-looking wrong number.
    """
    total = 0
    for unit in DAMAGE_UNITS.split(","):
        try:
            out = subprocess.run(
                ["journalctl", "-u", unit.strip(), "--since", f"{hours} hours ago",
                 "--no-pager", "-o", "cat"],
                capture_output=True, text=True, timeout=120,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        total += out.count("OUT_OF_USAGE_CREDIT")

    quick = False
    log = APP / "logs" / "tracker_quick.log"
    try:
        if log.exists():
            tail = log.read_text(errors="replace")[-40000:]
            # Only the last run in the file, so an old failure doesn't linger.
            last_run = tail.rsplit("=" * 70, 2)[-1]
            quick = "OUT_OF_USAGE_CREDIT" in last_run
    except OSError:
        pass
    return total, quick


def main() -> None:
    load_env()
    report_only = "--report" in sys.argv
    force = "--force" in sys.argv

    low_limit = int(os.environ.get("ODDS_QUOTA_LOW_REMAINING", "2000"))

    remaining, used, cost = probe()
    if remaining is None:
        # Unreachable API or missing key: say so and stop. Reporting this as a
        # quota problem would be the same conflation this watchdog exists to end.
        print("could not read quota headers — no alert sent")
        return

    print(f"remaining: {remaining}   used: {used}   this probe cost: {cost}")

    state = load_state()
    now = time.time()

    history = [s for s in state.get("history", []) if isinstance(s, list) and len(s) == 2]
    history = [s for s in history if now - s[0] < HISTORY_KEEP_SECS]
    if used is not None:
        history = [s for s in history if s[1] <= used]   # drop pre-reset samples
        history.append([now, used])
    state["history"] = history

    per_day = days_left = None
    if used is not None and len(history) >= 2:
        span_days = (now - history[0][0]) / 86400
        if span_days >= 0.5:
            per_day = (used - history[0][1]) / span_days
            if per_day > 0:
                days_left = remaining / per_day

    rate_line = (
        f"Burn rate: {per_day:.0f} req/day over the last {(now - history[0][0]) / 86400:.1f}d"
        + (f" → ~{days_left:.1f} days left" if days_left is not None else "")
        if per_day is not None else
        "Burn rate: not enough history yet (needs ~12h of samples)"
    )
    print(rate_line)

    if report_only:
        save_state(state)
        return

    sent_any = False

    if force or remaining <= 0:
        if force or now - state.get("out_alert_at", 0) > OUT_DEBOUNCE_SECS:
            failed, quick_failed = damage_signals(1)
            seen = f"Failed pricing attempts logged in the last hour: {failed}"
            if quick_failed:
                seen += "\nThe latest fast-path tracker run also failed on quota."
            msg = (
                f"🛑 Odds API quota exhausted\n\n"
                f"Used {used}, remaining {remaining}.\n"
                f"{seen}\n\n"
                f"Picks are being posted with NO price right now, and the miss is "
                f"cached — the tracker fetches odds once per pick, so every pick "
                f"stranded during this outage stays priceless even after the quota "
                f"resets. The 401 is recorded as match_type=no_game, which looks "
                f"identical to a genuinely missing event.\n\n"
                f"Repair after a reset needs historical closing lines "
                f"(odds._try_pregame), not a re-fetch — by then the games have "
                f"started and a re-fetch returns live prices."
            )
            if send(msg):
                state["out_alert_at"] = now
                sent_any = True

    elif force or remaining < low_limit:
        if force or now - state.get("low_alert_at", 0) > LOW_DEBOUNCE_SECS:
            msg = (
                f"📉 Odds API quota running low\n\n"
                f"Remaining {remaining} of {(used or 0) + remaining} — threshold {low_limit}.\n"
                f"{rate_line}\n\n"
                f"At zero, picks price as no_game and the empty result is cached "
                f"permanently. Raise the plan or cut request volume before then."
            )
            if send(msg):
                state["low_alert_at"] = now
                sent_any = True

    save_state(state)
    if sent_any:
        print("alert sent")


if __name__ == "__main__":
    main()
