#!/usr/bin/env python3
"""Offline tests: the event watchdogs' agent-first fold (operator 2026-10-11).

With its <X>_HEALTHCHECK_URL set, each watchdog (deploy/mem_watchdog.py,
deploy/claude_spend_watchdog.py, deploy/odds_quota_watchdog.py) stops DMing
and signals its dedicated agent-first check instead: /fail with the diagnosis
as body while the condition holds (every run — healthchecks only alerts on
the flip), a bare success ping on clean runs, and (odds-quota) NO ping at all
when the probe can't read the usage headers — silence trips the missed-ping
alarm, which is the honest signal. URL unset = the legacy direct-DM behavior,
so desktop clones keep working. hc-heartbeat's COVERED carries the three
units; hc_repair's KEY_HINTS carries their agent context.

    ~/venv/bin/python scripts/test_watchdog_fold.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TMP = Path(tempfile.mkdtemp(prefix="wd_fold_"))
URL_KEYS = ("MEM_WATCHDOG_HEALTHCHECK_URL", "CLAUDE_SPEND_HEALTHCHECK_URL",
            "ODDS_QUOTA_HEALTHCHECK_URL")
for k in URL_KEYS + ("WATCHDOG_BOT_TOKEN", "WATCHDOG_USER_ID"):
    os.environ.pop(k, None)


def harness(mod, url_key):
    """Neuter env/network/state; record pings and DMs."""
    mod.load_env = lambda: None
    mod.STATE = TMP / f"{mod.__name__}_state.json"
    mod.STATE.unlink(missing_ok=True)
    pings, dms = [], []
    real_ping = mod.ping_check

    def fake_ping(suffix="", body=""):
        if not os.environ.get(url_key, ""):
            return False
        pings.append((suffix, body))
        return True

    mod.ping_check = fake_ping
    mod.send = lambda text: dms.append(text) or True
    return pings, dms, real_ping


# ── ping_check itself (one module stands for all three) ─────────────────────
mem = load("mem_watchdog")
pings, dms, real_ping = harness(mem, "MEM_WATCHDOG_HEALTHCHECK_URL")
check("ping_check with no URL is a no-op returning False", real_ping("/fail", "x") is False)
captured = []


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def _fake_urlopen(req, timeout=None):
    captured.append((req.full_url, req.data))
    return _Resp()


os.environ["MEM_WATCHDOG_HEALTHCHECK_URL"] = "https://hc-ping.com/mem-uuid"
saved_urlopen = mem.urllib.request.urlopen
mem.urllib.request.urlopen = _fake_urlopen
try:
    ok = real_ping("/fail", "boom")
finally:
    mem.urllib.request.urlopen = saved_urlopen
check("ping_check posts the body to url+suffix",
      ok is True and captured == [("https://hc-ping.com/mem-uuid/fail", b"boom")],
      f"{captured}")

# ── mem_watchdog routing ─────────────────────────────────────────────────────
mem.meminfo = lambda: {"mem_total_mb": 957, "mem_avail_mb": 120,
                       "swap_total_mb": 2047, "swap_used_mb": 300}
mem.scan_oom = lambda since: ["grade_daemon"]
sys.argv = ["mem_watchdog.py"]
mem.main()
check("OOM with URL set → one /fail ping naming the process, no DM",
      len(pings) == 1 and pings[0][0] == "/fail"
      and "grade_daemon" in pings[0][1] and "RAM 120MB" in pings[0][1] and dms == [],
      f"{pings} {dms}")

mem.scan_oom = lambda since: []
pings.clear()
mem.main()
check("OOM hold: next clean run still /fails (event outlives the 10-min debounce)",
      len(pings) == 1 and pings[0][0] == "/fail", f"{pings}")

st = json.loads(mem.STATE.read_text())
st["oom_incident"]["at"] = time.time() - mem.OOM_FAIL_HOLD_SECS - 60
mem.STATE.write_text(json.dumps(st))
pings.clear()
mem.main()
check("expired hold + clean memory → success ping with the summary",
      len(pings) == 1 and pings[0][0] == "" and "swap 300/2047MB" in pings[0][1],
      f"{pings}")

os.environ.pop("MEM_WATCHDOG_HEALTHCHECK_URL")
mem.STATE.unlink(missing_ok=True)
mem.scan_oom = lambda since: ["sauce_daily"]
pings.clear()
mem.main()
check("URL unset → legacy 🔴 DM path (desktop clones unchanged)",
      pings == [] and len(dms) == 1 and dms[0].startswith("🔴") and "sauce_daily" in dms[0],
      f"{pings} {dms}")

# ── claude_spend_watchdog routing ────────────────────────────────────────────
spend = load("claude_spend_watchdog")
pings, dms, _ = harness(spend, "CLAUDE_SPEND_HEALTHCHECK_URL")
now = time.time()
ledger = TMP / "claude_spend.jsonl"
spend.LEDGER = ledger


def write_ledger(hour_usd):
    rows = [{"ts": now - 25 * 3600, "usd": 0.01, "source": "old"},
            {"ts": now - 600, "usd": hour_usd, "source": "grade-daemon"}]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))


os.environ["CLAUDE_SPEND_HEALTHCHECK_URL"] = "set"
os.environ["CLAUDE_SPEND_DAY_ALERT_USD"] = "3.00"
os.environ["CLAUDE_SPEND_HOUR_ALERT_USD"] = "0.60"
sys.argv = ["claude_spend_watchdog.py"]
write_ledger(0.90)
spend.main()
check("hour spend over limit → /fail with the breakdown, no DM",
      len(pings) == 1 and pings[0][0] == "/fail" and "grade-daemon: $0.90" in pings[0][1]
      and dms == [], f"{pings} {dms}")

write_ledger(0.05)
pings.clear()
spend.main()
check("spend back under → success ping carrying the figures",
      len(pings) == 1 and pings[0][0] == "" and "last hour $0.05" in pings[0][1],
      f"{pings}")

os.environ.pop("CLAUDE_SPEND_HEALTHCHECK_URL")
write_ledger(0.90)
pings.clear()
spend.main()
check("URL unset → legacy ⚡ DM", pings == [] and len(dms) == 1 and dms[0].startswith("⚡"),
      f"{pings} {dms}")

# ── odds_quota_watchdog routing ──────────────────────────────────────────────
quota = load("odds_quota_watchdog")
pings, dms, _ = harness(quota, "ODDS_QUOTA_HEALTHCHECK_URL")
quota.damage_signals = lambda hours=1: (3, True)
os.environ["ODDS_QUOTA_HEALTHCHECK_URL"] = "set"
sys.argv = ["odds_quota_watchdog.py"]

quota.probe = lambda: (0, 500, 0)
quota.main()
check("quota out → /fail with damage count, no DM",
      len(pings) == 1 and pings[0][0] == "/fail" and "remaining 0" in pings[0][1]
      and "failed pricing attempts in the last hour: 3" in pings[0][1] and dms == [],
      f"{pings} {dms}")

quota.probe = lambda: (5000, 100, 0)
pings.clear()
quota.main()
check("quota ok → success ping with remaining",
      len(pings) == 1 and pings[0][0] == "" and "remaining 5000" in pings[0][1], f"{pings}")

quota.probe = lambda: (None, None, None)
pings.clear()
quota.main()
check("unreadable probe pings NOTHING (missed pings are the signal)",
      pings == [] and dms == [], f"{pings} {dms}")

os.environ.pop("ODDS_QUOTA_HEALTHCHECK_URL")
quota.probe = lambda: (0, 500, 0)
quota.STATE.unlink(missing_ok=True)
pings.clear()
quota.main()
check("URL unset → legacy 🛑 DM", pings == [] and len(dms) == 1 and dms[0].startswith("🛑"),
      f"{pings} {dms}")

# ── wiring: heartbeat COVERED + hc-repair hints ──────────────────────────────
hb = load("hc_heartbeat")
check("heartbeat COVERED carries the three watchdog units",
      hb.COVERED.get("mem-watchdog.timer") == "MEM_WATCHDOG_HEALTHCHECK_URL"
      and hb.COVERED.get("claude-spend-watchdog.timer") == "CLAUDE_SPEND_HEALTHCHECK_URL"
      and hb.COVERED.get("odds-quota-watchdog.timer") == "ODDS_QUOTA_HEALTHCHECK_URL")
from scripts import hc_repair as hr  # noqa: E402
check("hc-repair has agent hints for the three watchdog checks",
      all(k in hr.KEY_HINTS for k in URL_KEYS))

print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
