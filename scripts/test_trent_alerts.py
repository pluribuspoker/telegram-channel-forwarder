#!/usr/bin/env python3
"""Regression: Trent watcher alerts must be truthful.

Since the consolidation (operator 2026-10-11) the watcher sends ONE DM — the
ℹ️ degradation early warning. An outage is the agent-first pipeline's job:
the run prints FATAL + remedy to the journal, exits non-zero, the runner
/fails the Trent monitor check, and hc-repair's agent + card notify the
operator. 2026-09-24 context (why remedy precision matters): X's staged
web-build rollout took the watcher down and the bootstrap error text blamed
the cookies until diagnose_failure separated the cases.

Pins: _report_fetch_failure never DMs and prints the remedy for the agent,
the runner is free of repair triggers and attempt-gating env, the ℹ️ early
warning (4+ runs in 24h, once per 24h, 48h prune), and state round-trips.

Offline and silent: _send_dm is stubbed, httpx.post raises, WATCHDOG_* are
unset, the state file lives in a temp dir, and the runner copy gets a minimal
env (no healthcheck URL). Needs .env (trent_watcher reads it at import).

    ~/venv/bin/python scripts/test_trent_alerts.py
"""
import asyncio
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import trent_watcher as tw  # noqa: E402
from scripts import x_client as xc  # noqa: E402
from twscrape import xclid as _xclid  # noqa: E402

# --- Nothing in this file may reach a real operator ---------------------------
for _key in ("WATCHDOG_BOT_TOKEN", "WATCHDOG_USER_ID"):
    os.environ.pop(_key, None)


def _no_network(*a, **k):
    raise AssertionError("test attempted a real HTTP request")


tw.httpx.post = _no_network
TMP = Path(tempfile.mkdtemp(prefix="trent_alerts_"))
tw._ALERT_STATE = TMP / "state.json"

real_send_dm = tw._send_dm
sent: list[str] = []
send_ok = [True]


def _fake_send(text: str) -> bool:
    sent.append(text)
    return send_ok[0]


tw._send_dm = _fake_send

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def _reset(state: dict | None = None):
    sent.clear()
    send_ok[0] = True
    tw._ALERT_STATE.unlink(missing_ok=True)
    if state is not None:
        tw._ALERT_STATE.write_text(json.dumps(state))


def _state():
    return json.loads(tw._ALERT_STATE.read_text()) if tw._ALERT_STATE.exists() else None


T = datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc)
MIN15 = timedelta(minutes=15)
CLEAN = {"create_failures": 0, "fallback_page": None, "heuristic": False}
FALLBACK = {"create_failures": 0, "fallback_page": "https://x.com/explore", "heuristic": False}
RETRIED = {"create_failures": 2, "fallback_page": None, "heuristic": False}
HEURISTIC = {"create_failures": 0, "fallback_page": None, "heuristic": True}

# --- The production bootstrap-failure error, end to end ----------------------
# Real _fetch_impl -> diagnose_failure -> patched XClIdGen.create -> REAL
# load_keys/_parse_anim_idx/_find_indices_url over all 7 _XCLID_PAGES, with only
# the network stubbed (a page linking one x-web entry whose body names nothing).
ENTRY = "https://abs.twimg.com/x-web/x-web/entry-client-logged-out-ALRT.js"


class _Resp:
    def __init__(self, text):
        self.text = text


class _CdnClient:
    async def get(self, url, *a, **k):
        return _Resp("export const noop=1;")

    async def aclose(self):
        pass


async def _stub_page(url, clt):
    return f'<html><script type="module" src="{ENTRY}"></script></html>'


class _NoUserAPI:
    async def user_by_login(self, name):
        return None


async def _no_user_api():
    return _NoUserAPI()


saved = {k: getattr(_xclid, k) for k in ("_make_client", "get_tw_page_text")}
saved_build_api = tw.build_api
_xclid._make_client = lambda: _CdnClient()
_xclid.get_tw_page_text = _stub_page
tw.build_api = _no_user_api
try:
    asyncio.run(tw._fetch_impl(T, 20))
    boot_err = None
except tw._XFetchError as e:
    boot_err = e
finally:
    for k, v in saved.items():
        setattr(_xclid, k, v)
    tw.build_api = saved_build_api

check("stubbed total bootstrap failure raises _XFetchError(kind=bootstrap)",
      boot_err is not None and boot_err.kind == "bootstrap", repr(boot_err))
full = str(boot_err)
check("fixture really is the 7-page shape", full.count(" -> ") == 7, f"{full.count(' -> ')} entries")

# (b) _report_fetch_failure: journal-only — FATAL + remedy, never a DM ---------
downed = (T - timedelta(hours=1)).isoformat()
_reset()
err_out = io.StringIO()
with contextlib.redirect_stderr(err_out):
    tw._report_fetch_failure(boot_err)
check("a failing run sends NO DM (the agent-first pipeline notifies)",
      sent == [], f"{sent}")
check("a failing run writes no alert state", _state() is None, f"{_state()}")
logged = err_out.getvalue()
check("FATAL line carries the kind and full error",
      logged.startswith("FATAL [bootstrap]:") and logged.count(" -> ") == 7,
      logged[:120])
check("remedy line is in the journal for the hc-repair agent",
      f"remedy: {boot_err.remedy}" in logged, logged[-200:])
check("bootstrap remedy blames the code, not the cookies",
      "_INDICES_FILE_RE" in boot_err.remedy and "Do NOT refresh the cookies" in boot_err.remedy)

# Legacy 🔴-era state keys are ignored, never resurrected into DMs.
_reset({"last_auth_alert": downed, "down_alerted_at": downed})
tw._after_conclusive_fetch(CLEAN, now=T)
check("legacy down_alerted_at state triggers no recovery DM", sent == [], f"{sent}")
check("legacy keys survive untouched (unknown-key policy)",
      (_state() or {}).get("down_alerted_at") == downed, f"{_state()}")


async def _fetch_ok(since, limit):
    return [{"id": "1"}]


async def _fetch_timeout(since, limit):
    raise asyncio.TimeoutError()


async def _fetch_boot(since, limit):
    raise tw._XFetchError("x", kind="bootstrap", remedy="y")


saved_impl = tw._fetch_impl
try:
    tw._fetch_impl = _fetch_ok
    check("completed fetch is conclusive", asyncio.run(tw.fetch_recent_tweets(T)) == ([{"id": "1"}], True))
    tw._fetch_impl = _fetch_timeout
    check("rate-limit timeout is inconclusive", asyncio.run(tw.fetch_recent_tweets(T)) == ([], False))
    tw._fetch_impl = _fetch_boot
    try:
        asyncio.run(tw.fetch_recent_tweets(T))
        check("_XFetchError still propagates", False, "swallowed")
    except tw._XFetchError:
        check("_XFetchError still propagates", True)
finally:
    tw._fetch_impl = saved_impl

# (d) ℹ️ degradation early warning -----------------------------------------------
_reset()
for i in range(10):
    tw._after_conclusive_fetch(CLEAN, now=T + i * MIN15)
check("clean runs record nothing and write nothing", sent == [] and _state() is None, f"{_state()}")

_reset()
for i in range(3):
    tw._after_conclusive_fetch(FALLBACK, now=T + i * MIN15)
check("3 degraded runs in 24h: no ℹ️ yet", sent == [], f"{sent}")
tw._after_conclusive_fetch(FALLBACK, now=T + 3 * MIN15)
check("the 4th degraded run in 24h sends ℹ️", len(sent) == 1 and sent[0].startswith("ℹ️"), f"{sent}")
check("ℹ️ carries the 24h count and the fallback page",
      bool(sent) and "4 runs in the last 24h" in sent[0] and "https://x.com/explore" in sent[0],
      sent[0][:160] if sent else "")
check("ℹ️ says the watcher is still healthy and a code fix may follow",
      bool(sent) and "still healthy and self-recovering" in sent[0] and "code fix" in sent[0])
tw._after_conclusive_fetch(FALLBACK, now=T + 4 * MIN15)
check("no repeat ℹ️ within 24h", len(sent) == 1, f"{len(sent)} DMs")

# An extended rollout: a degraded run every 15 min for 25h -> exactly one more
# ℹ️, at the first run 24h after the first one.
_reset()
for i in range(4 * 25 + 1):
    tw._after_conclusive_fetch(FALLBACK, now=T + i * MIN15)
st = _state() or {}
check("25h of degraded runs send exactly two ℹ️ (24h re-arm)", len(sent) == 2, f"{len(sent)} DMs")
check("second ℹ️ lands exactly at the re-arm",
      st.get("last_degraded_alert") == (T + 3 * MIN15 + timedelta(hours=24)).isoformat(),
      st.get("last_degraded_alert", ""))

_reset()
for i, rep in enumerate((RETRIED, RETRIED, RETRIED, HEURISTIC)):
    tw._after_conclusive_fetch(rep, now=T + i * MIN15)
check("failed create attempts alone count as degradation", len(sent) == 1, f"{len(sent)} DMs")
check("ℹ️ names the content heuristic when it was used",
      bool(sent) and "content heuristic" in sent[0] and "_INDICES_FILE_RE" in sent[0], sent[0] if sent else "")

_reset()
for i in range(3):
    tw._after_conclusive_fetch(FALLBACK, now=T + i * MIN15)
send_ok[0] = False
tw._after_conclusive_fetch(FALLBACK, now=T + 3 * MIN15)
check("a failed ℹ️ send leaves the alert unarmed", "last_degraded_alert" not in (_state() or {}))
send_ok[0] = True
tw._after_conclusive_fetch(CLEAN, now=T + 4 * MIN15)
check("the next conclusive run retries the ℹ️", len(sent) == 2 and "last_degraded_alert" in (_state() or {}),
      f"{len(sent)} / {_state()}")

_reset()
for i in range(4):
    tw._after_conclusive_fetch(FALLBACK, now=T + i * MIN15)
tw._after_conclusive_fetch(CLEAN, now=T + 3 * MIN15 + timedelta(hours=47))
check("events younger than 48h are kept", len((_state() or {}).get("degraded_events", [])) == 4)
tw._after_conclusive_fetch(CLEAN, now=T + timedelta(hours=48, minutes=1))
check("events older than 48h are pruned", len((_state() or {}).get("degraded_events", [])) == 3,
      f"{(_state() or {}).get('degraded_events')}")
tw._after_conclusive_fetch(CLEAN, now=T + timedelta(hours=49))
check("... all of them, eventually", (_state() or {}).get("degraded_events") == [], f"{_state()}")
check("stale events never re-page", len(sent) == 1, f"{len(sent)} DMs")

# (e) state round-trips ------------------------------------------------------------
future = {"future_key": {"nested": [1, 2]}, "last_auth_alert": "2020-01-01T00:00:00+00:00"}
_reset(dict(future))
tw._after_conclusive_fetch(FALLBACK, now=T)
check("ℹ️ bookkeeping keeps unknown state keys",
      (_state() or {}).get("future_key") == future["future_key"], f"{_state()}")

for label, raw in (("corrupt JSON", "{not json"), ("non-dict JSON", "[1, 2]")):
    _reset()
    tw._ALERT_STATE.write_text(raw)
    check(f"{label} loads as empty state", tw._load_state() == {})
    tw._after_conclusive_fetch(FALLBACK, now=T)
    check(f"{label} is replaced by a valid state on the next save",
          (_state() or {}).get("degraded_events") == [T.isoformat()],
          f"{tw._ALERT_STATE.read_text()}")

_reset({"degraded_events": "not-a-list", "last_degraded_alert": 7})
tw._after_conclusive_fetch(FALLBACK, now=T)
check("garbage fields are tolerated and repaired",
      (_state() or {}).get("degraded_events") == [T.isoformat()], f"{_state()}")

# (f) the real _send_dm: plain text, no parse_mode --------------------------------
calls = []


class _R:
    def __init__(self, code):
        self.status_code = code


code = [200]


def _post(url, data=None, timeout=None, **kw):
    calls.append((url, data, kw))
    if code[0] is None:
        raise OSError("network down")
    return _R(code[0])


tw.httpx.post = _post
try:
    check("no WATCHDOG_* -> False, no request", real_send_dm("x") is False and calls == [])
    os.environ["WATCHDOG_BOT_TOKEN"], os.environ["WATCHDOG_USER_ID"] = "TEST-TOKEN", "42"
    check("HTTP 200 -> True", real_send_dm("hello") is True)
    check("DM is plain text (no parse_mode)",
          calls and calls[-1][1] == {"chat_id": "42", "text": "hello"} and not calls[-1][2], f"{calls[-1:]}")
    code[0] = 500
    check("HTTP 500 -> False", real_send_dm("x") is False)
    code[0] = None
    check("network error -> False", real_send_dm("x") is False)
finally:
    tw.httpx.post = _no_network
    os.environ.pop("WATCHDOG_BOT_TOKEN", None)
    os.environ.pop("WATCHDOG_USER_ID", None)

# (g) run_trent_watcher.sh: retry loop only — no repair trigger, no gating env ----
# A copy with every live path swapped for the temp dir and the 60s backoff cut,
# run with a minimal env (no TRENT_HEALTHCHECK_URL, so ping_hc is a no-op).
runner = (ROOT / "run_trent_watcher.sh").read_text()
check("runner is free of repair triggers and attempt-gating env (consolidated)",
      "trent-repair" not in runner and "sudo" not in runner
      and "TRENT_FINAL_ATTEMPT" not in runner)
live_bits = ('APP_DIR="/home/forwarder/app"', 'PYTHON="/home/forwarder/venv/bin/python"',
             'LOGFILE="/tmp/trent_watcher_last_run.log"', "sleep 60")
check("runner has the expected live paths to swap", all(runner.count(b) == 1 for b in live_bits))
stub = TMP / "stub_python"
stub.write_text('#!/bin/bash\necho run >> "$REC"\nexit "$STUB_EXIT"\n')
stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
safe = (runner.replace(live_bits[0], f'APP_DIR="{TMP}"')
              .replace(live_bits[1], f'PYTHON="{stub}"')
              .replace(live_bits[2], f'LOGFILE="{TMP}/last_run.log"')
              .replace(live_bits[3], "sleep 0"))
assert "/home/forwarder/app" not in safe and "/tmp/trent_watcher_last_run.log" not in safe
assert "/home/forwarder/venv" not in safe
(TMP / "runner.sh").write_text(safe)
for exit_code, want_rc, want_runs in (("1", 1, 2), ("0", 0, 1)):
    rec = TMP / f"rec_{exit_code}"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(TMP), "REC": str(rec), "STUB_EXIT": exit_code}
    r = subprocess.run(["bash", str(TMP / "runner.sh")], env=env, capture_output=True, text=True, timeout=60)
    got = len(rec.read_text().split()) if rec.exists() else 0
    check(f"runner retries once then exits (watcher exit {exit_code})",
          r.returncode == want_rc and got == want_runs,
          f"rc={r.returncode} runs={got} {r.stdout[-120:]}")
check("runner keeps its exec bit", os.access(ROOT / "run_trent_watcher.sh", os.X_OK))

# (h) main() wiring, fully faked: temp DB (never the live picks.db), fake X API.


class _EmptyTimelineAPI:
    async def user_by_login(self, name):
        return SimpleNamespace(id=1, username=tw.USERNAME)

    async def user_tweets(self, uid, limit=-1):
        for t in ():
            yield t


async def _empty_api():
    return _EmptyTimelineAPI()


async def _diag_bootstrap():
    return ("bootstrap", "stub detail")


def _main(*argv) -> int:
    sys.argv = ["trent_watcher.py", *argv]
    try:
        asyncio.run(tw.main())
        return 0
    except SystemExit as e:
        return e.code


saved_main = (tw.DB_PATH, tw.build_api, tw.diagnose_failure, list(sys.argv))
tw.DB_PATH = str(TMP / "picks.db")
xc._BOOTSTRAP_REPORT.update(create_failures=0, fallback_page=None, heuristic=False)
try:
    tw.build_api = _empty_api
    _reset({"last_auth_alert": downed, "down_alerted_at": downed})
    rc = _main("--dry-run")
    check("main --dry-run: conclusive, no DM", rc == 0 and sent == [], f"rc={rc} {sent}")
    rc = _main()
    check("main with legacy 🔴-era state: conclusive run, still no DM",
          rc == 0 and sent == [], f"rc={rc} {sent}")

    tw.build_api = _no_user_api
    tw.diagnose_failure = _diag_bootstrap
    _reset()
    rc = _main()
    check("main, failing run: exit 1 and NO DM (the pipeline notifies)",
          rc == 1 and sent == [], f"rc={rc} {sent}")
finally:
    tw.DB_PATH, tw.build_api, tw.diagnose_failure, sys.argv[:] = saved_main

shutil.rmtree(TMP, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
