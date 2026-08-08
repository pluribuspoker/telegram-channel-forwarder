"""Claude API spend watchdog — DMs the operator when daily spend spikes.

Stays silent unless a threshold trips, then alerts through the watchdog bot
(same bot as mem_watchdog / claude_watchdog_bot).

Why this exists: on 2026-07-22 a single mis-parsed pick started being re-graded
every cycle by both the daemon and the tracker. It cost ~$9.40/day and ran for
five days before anyone thought to look — there was no signal at all, because
spend is only visible by grepping `[Claude] $` out of journald. Code guards
(see common.should_skip_unknown) stop that specific mechanism; this catches the
next cost blowup whose cause we haven't thought of yet.

  * 💸 daily  — trailing 24h spend over CLAUDE_SPEND_DAY_ALERT_USD
  * ⚡ hourly — trailing 1h spend over CLAUDE_SPEND_HOUR_ALERT_USD, which
    catches a fast leak long before the daily figure crosses.

Spend is read from logs/claude_spend.jsonl, which ai.py appends to at the single
choke point every Claude call passes through. It replaced a journald scan of four
systemd units that could not see any other caller: sauce_daily (cron) and the
listener's fast-path tracker (a subprocess) were together ~$0.14/day against the
~$0.15/day the scan could see, so the reported figure ran ~40% low and the $3/day
threshold really tripped nearer $5. journald remains the fallback for a window the
ledger doesn't reach back through yet — the two are never summed, since for those
four units they describe the same calls.

Reuses WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID from .env. State (for debounce)
lives in ~/.claude_spend_watchdog_state.json.

Usage:
    python deploy/claude_spend_watchdog.py            # normal (silent unless tripped)
    python deploy/claude_spend_watchdog.py --report   # print the numbers, send nothing
    python deploy/claude_spend_watchdog.py --force    # send even if under threshold / debounced
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
STATE = Path.home() / ".claude_spend_watchdog_state.json"

DEFAULT_UNITS = "grade-daemon,telegram-tracker,telegram-forwarder,trent-monitor"
COST_RE = re.compile(r"\[Claude\]\s+\$([0-9]+(?:\.[0-9]+)?)")

# Preferred source: ai.py appends one record per call here, at the single choke
# point every Claude call passes through, so it counts callers that aren't
# systemd units (sauce_daily via cron, the listener's fast-path tracker, manual
# backfill scripts) — all of which the journald scan below cannot see at all.
LEDGER = APP / "logs" / "claude_spend.jsonl"
LEDGER_KEEP_DAYS = int(os.environ.get("CLAUDE_SPEND_LEDGER_DAYS", "30"))

DAY_DEBOUNCE_SECS = 12 * 60 * 60   # at most one 💸 alert per 12h
HOUR_DEBOUNCE_SECS = 6 * 60 * 60   # at most one ⚡ alert per 6h


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


def unit_spend(unit: str, since: str) -> tuple[float, int]:
    """(dollars, call_count) logged by `unit` since `since`."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "--since", since, "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f"journalctl {unit} failed: {e}", file=sys.stderr)
        return 0.0, 0
    vals = [float(m) for m in COST_RE.findall(out)]
    return sum(vals), len(vals)


def collect(units: list[str], since: str) -> tuple[float, int, dict[str, tuple[float, int]]]:
    per: dict[str, tuple[float, int]] = {}
    total = 0.0
    calls = 0
    for u in units:
        d, n = unit_spend(u, since)
        if n:
            per[u] = (d, n)
        total += d
        calls += n
    return total, calls, per


def read_ledger() -> tuple[list[tuple[float, float, str]], float | None]:
    """(records, earliest_ts). records are (ts, usd, source); earliest_ts is None
    if the ledger is absent or unreadable."""
    try:
        raw = LEDGER.read_text(errors="replace")
    except OSError:
        return [], None
    recs: list[tuple[float, float, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            recs.append((float(d["ts"]), float(d["usd"]), str(d.get("source", "unknown"))))
        except (ValueError, KeyError, TypeError):
            continue  # a torn line from a concurrent append is not worth failing over
    if not recs:
        return [], None
    return recs, min(r[0] for r in recs)


def ledger_window(recs: list[tuple[float, float, str]], since_ts: float
                  ) -> tuple[float, int, dict[str, tuple[float, int]]]:
    per: dict[str, tuple[float, int]] = {}
    total = 0.0
    calls = 0
    for ts, usd, src in recs:
        if ts < since_ts:
            continue
        d, n = per.get(src, (0.0, 0))
        per[src] = (d + usd, n + 1)
        total += usd
        calls += 1
    return total, calls, per


def prune_ledger(recs: list[tuple[float, float, str]], now: float) -> None:
    """Drop records older than LEDGER_KEEP_DAYS. Rewrite-and-replace, so a
    concurrent append is never interleaved into a half-written file."""
    cutoff = now - LEDGER_KEEP_DAYS * 86400
    keep = [r for r in recs if r[0] >= cutoff]
    if len(keep) == len(recs):
        return
    try:
        tmp = LEDGER.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(
            json.dumps({"ts": ts, "usd": usd, "source": src}, separators=(",", ":")) + "\n"
            for ts, usd, src in keep))
        os.replace(tmp, LEDGER)
    except OSError as e:
        print(f"ledger prune failed: {e}", file=sys.stderr)


def window(units: list[str], recs: list[tuple[float, float, str]], earliest: float | None,
           now: float, hours: float, since_str: str
           ) -> tuple[float, int, dict[str, tuple[float, int]], str]:
    """Spend over the trailing `hours`, plus which source produced it.

    The ledger is authoritative *only* once it reaches back past the start of the
    window — otherwise a freshly-deployed ledger would report near-zero and the
    alert would go quiet exactly when it still had journald history to use. The
    two are never summed: for the four systemd units they describe the same calls.
    """
    since_ts = now - hours * 3600
    if earliest is not None and earliest <= since_ts:
        t, c, per = ledger_window(recs, since_ts)
        return t, c, per, "ledger"
    t, c, per = collect(units, since_str)
    note = "journald (ledger too new)" if earliest is not None else "journald (no ledger)"
    return t, c, per, note


def fmt_breakdown(per: dict[str, tuple[float, int]]) -> str:
    if not per:
        return "  (no Claude calls logged)"
    rows = sorted(per.items(), key=lambda kv: kv[1][0], reverse=True)
    return "\n".join(f"  {u}: ${d:.2f} ({n} calls)" for u, (d, n) in rows)


def main() -> None:
    load_env()
    report_only = "--report" in sys.argv
    force = "--force" in sys.argv

    units = [u.strip() for u in os.environ.get("CLAUDE_SPEND_UNITS", DEFAULT_UNITS).split(",") if u.strip()]
    day_limit = float(os.environ.get("CLAUDE_SPEND_DAY_ALERT_USD", "3.00"))
    hour_limit = float(os.environ.get("CLAUDE_SPEND_HOUR_ALERT_USD", "0.60"))

    now = time.time()
    recs, earliest = read_ledger()

    day_total, day_calls, day_per, day_src = window(units, recs, earliest, now, 24, "24 hours ago")
    hour_total, hour_calls, hour_per, hour_src = window(units, recs, earliest, now, 1, "1 hour ago")

    print(f"trailing 24h: ${day_total:.2f} ({day_calls} calls, limit ${day_limit:.2f}) [{day_src}]")
    print(fmt_breakdown(day_per))
    print(f"trailing 1h : ${hour_total:.2f} ({hour_calls} calls, limit ${hour_limit:.2f}) [{hour_src}]")
    print(fmt_breakdown(hour_per))

    if report_only:
        return

    if recs:
        prune_ledger(recs, now)

    state = load_state()
    sent_any = False

    # A steady per-cycle rate is the signature of a re-grade loop rather than
    # real volume, so quote the rate — it's what makes the cause recognisable.
    if force or hour_total > hour_limit:
        if force or now - state.get("hour_alert_at", 0) > HOUR_DEBOUNCE_SECS:
            msg = (
                f"⚡ Claude spend spike\n\n"
                f"Last hour: ${hour_total:.2f} ({hour_calls} calls) — limit ${hour_limit:.2f}\n"
                f"{fmt_breakdown(hour_per)}\n\n"
                f"Trailing 24h: ${day_total:.2f} ({day_calls} calls)\n\n"
                f"A steady ~1 call/min with nothing resolving means a pick is being "
                f"re-graded in a loop, not real volume.\n"
                f"Source: {hour_src}\n"
                f"Check: journalctl -u grade-daemon --since '1 hour ago' | grep -B6 '\\[Claude\\]'"
            )
            if send(msg):
                state["hour_alert_at"] = now
                sent_any = True

    # A 24h window keeps reporting a burst for a full day after it stops, so a
    # fix deployed mid-window still alerts as if it were live. Quote the current
    # hour alongside it: near-zero there means the cause is already gone and
    # this is the window draining, not a leak to go chase.
    if force or day_total > day_limit:
        if force or now - state.get("day_alert_at", 0) > DAY_DEBOUNCE_SECS:
            if hour_total < day_total / 24:
                verdict = "Spend has already stopped — 24h window is draining, not a live leak."
            else:
                verdict = "Still spending now. Normal baseline is well under $1/day."
            msg = (
                f"💸 Claude spend above threshold\n\n"
                f"Trailing 24h: ${day_total:.2f} ({day_calls} calls) — limit ${day_limit:.2f}\n"
                f"{fmt_breakdown(day_per)}\n\n"
                f"Last hour: ${hour_total:.2f} ({hour_calls} calls)\n\n"
                f"Projected month at this rate: ${day_total * 30:.0f}\n\n"
                f"Source: {day_src}\n"
                f"{verdict}"
            )
            if send(msg):
                state["day_alert_at"] = now
                sent_any = True

    if sent_any:
        save_state(state)


if __name__ == "__main__":
    main()
