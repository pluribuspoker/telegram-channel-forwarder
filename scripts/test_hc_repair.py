#!/usr/bin/env python3
"""Offline tests for the health-check auto-repair runner (scripts/hc_repair.py).

Pure decision logic only — target building (heartbeat fan-out per unit,
exclusions, env-key → unit mapping), debounce/cooldown/park gates, streak
reset on recovery, the daily cap, the result contract, prompt guardrails and
the invoker's model swap. No network, no claude binary, no DMs.

    python scripts/test_hc_repair.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import hc_repair as hr  # noqa: E402

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


NOW = datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc)
iso = lambda dt: dt.isoformat(timespec="seconds")  # noqa: E731

ENV = {
    "HEARTBEAT_JOBS_HEALTHCHECK_URL": "https://hc-ping.com/jobs-uuid",
    "GOD_JUDGE_HEALTHCHECK_URL": "https://hc-ping.com/judge-uuid",
    "ROOT_BACKUP_HEALTHCHECK_URL": "https://hc-ping.com/backup-uuid",
}
COVERED = {"god-judge.timer": "GOD_JUDGE_HEALTHCHECK_URL"}
CHECKS = [
    {"name": "VPS jobs", "uuid": "jobs-uuid", "status": "down"},
    {"name": "God judge", "uuid": "judge-uuid", "status": "down"},
    {"name": "Root backup", "uuid": "backup-uuid", "status": "up"},
    {"name": "Trent monitor", "uuid": "trent-uuid", "status": "down"},
    {"name": "Sauce watch", "uuid": "sauce-uuid", "status": "grace"},
]
DETAILS = {
    "jobs-uuid": {"since": iso(NOW - timedelta(minutes=30)),
                  "body": "sauce-watch.service: last run failed (status 1)\n"
                          "env-backup.path: inactive (not scheduled)"},
    "judge-uuid": {"since": iso(NOW - timedelta(minutes=30)), "body": ""},
}

# 1. Parsing helpers.
check("failing_units parses heartbeat body",
      hr.failing_units(DETAILS["jobs-uuid"]["body"]) == ["sauce-watch.service", "env-backup.path"])
check("failing_units ignores prose", hr.failing_units("all 9 ok\nsomething: else") == [])
check("down_since = newest down flip",
      hr.down_since([{"timestamp": "2026-09-27T00:30:16+00:00", "up": 1},
                     {"timestamp": "2026-09-27T00:25:18+00:00", "up": 0},
                     {"timestamp": "2026-09-20T00:00:00+00:00", "up": 0}])
      == datetime(2026, 9, 27, 0, 25, 18, tzinfo=timezone.utc))
check("down_since none", hr.down_since([{"timestamp": "x", "up": 1}]) is None)

# 2. Targets.
targets = hr.build_targets(CHECKS, env=ENV, details=DETAILS, covered=COVERED)
keys = [t["key"] for t in targets]
check("heartbeat group fans out per unit",
      keys[:2] == ["jobs-uuid:sauce-watch.service", "jobs-uuid:env-backup.path"], str(keys))
judge = next(t for t in targets if t["check"] == "God judge")
check("dedicated check maps env key → unit",
      judge["env_key"] == "GOD_JUDGE_HEALTHCHECK_URL" and judge["unit"] == "god-judge.timer")
check("up/grace checks are not targets", not any(t["check"] in ("Root backup", "Sauce watch")
                                                 for t in targets))
check("excluded check (own repair rung) skipped", not any(t["check"] == "Trent monitor"
                                                         for t in targets))
nobody = hr.build_targets([{"name": "VPS jobs", "uuid": "jobs-uuid", "status": "down"}],
                          env=ENV, details={"jobs-uuid": {"since": None, "body": ""}},
                          covered=COVERED)
check("heartbeat with no /fail body = heartbeat itself",
      nobody[0]["key"] == "jobs-uuid" and nobody[0]["unit"] == "hc-heartbeat.timer")
backup = hr.build_targets([{"name": "Root backup", "uuid": "backup-uuid", "status": "down"}],
                          env=ENV, details={}, covered=COVERED)[0]
check("cron check carries its hint", "backup.sh" in backup["hint"] and backup["unit"] == "")

# 3. Gates.
fresh = {"key": "judge-uuid", "since": iso(NOW - timedelta(minutes=4))}
check("debounce holds a 4-min outage", hr.gate({}, fresh, NOW)[0] == "skip")
check("--force skips the debounce", hr.gate({}, fresh, NOW, force=True)[0] == "run")
old = {"key": "judge-uuid", "since": iso(NOW - timedelta(minutes=30))}
check("30-min outage runs", hr.gate({}, old, NOW) == ("run", "ok"))
unit_t = {"key": "jobs-uuid:x.service", "since": iso(NOW - timedelta(hours=3))}
check("unit target debounces on its own first_seen, not the group flip",
      hr.gate({"first_seen": iso(NOW - timedelta(minutes=5))}, unit_t, NOW)[0] == "skip")
check("unit target without first_seen falls back to flip (no crash)",
      hr.gate({}, unit_t, NOW)[0] == "run")
check("no timestamps at all runs (fail-open)", hr.gate({}, {"key": "a"}, NOW)[0] == "run")
cool = {"last_spawn_at": iso(NOW - timedelta(hours=1))}
check("cooldown skips", hr.gate(cool, old, NOW)[0] == "skip")
check("--force bypasses cooldown", hr.gate(cool, old, NOW, force=True)[0] == "run")
parked = {"parked": True, "parked_reason": "needs_human"}
check("fresh park asks for the DM", hr.gate(parked, old, NOW)[0] == "capped")
check("park with DM sent is silent, even forced",
      hr.gate({**parked, "capped_dm_sent": True}, old, NOW, force=True)[0] == "skip")

# 4. Spawn accounting, settle, recovery.
st: dict = {}
hr.observe(st, targets, NOW)
check("observe stamps first_seen", all(st[k]["first_seen"] == iso(NOW) for k in keys))
hr.record_spawn(st, ["judge-uuid"], NOW)
check("spawn counts toward daily cap", hr.daily_spawns(st, NOW) == 1)
check("daily cap window is 24h", hr.daily_spawns(st, NOW + timedelta(hours=25)) == 0)
e = st["judge-uuid"]
hr.settle(e, "fixed_needs_verify")
check("1st non-final attempt stays armed", not e.get("parked"))
hr.record_spawn(st, ["judge-uuid"], NOW)
hr.settle(e, "transient_no_change")
check("attempt cap parks", e.get("parked") and "attempt cap" in e["parked_reason"])
e2: dict = {"attempts": 1}
hr.settle(e2, "needs_human")
check("needs_human parks immediately", e2.get("parked") and e2["parked_reason"] == "needs_human")
e3: dict = {"attempts": 2}
hr.settle(e3, "fixed_verified")
check("verified fix never parks", not e3.get("parked"))
# Recovery: judge comes back up → streak resets, cooldown kept.
hr.observe(st, [t for t in targets if t["key"] != "judge-uuid"], NOW + timedelta(minutes=5))
check("recovery clears park/attempts, keeps cooldown",
      not st["judge-uuid"].get("parked") and "attempts" not in st["judge-uuid"]
      and st["judge-uuid"]["last_spawn_at"] == iso(NOW))
hr.observe(st, [], NOW + timedelta(hours=7))
check("stale recovered entries pruned", "judge-uuid" not in st and "_spawns" in st)

# 5. Result contract.
two = targets[:2]
txt = ('done\nHC_REPAIR_RESULT: [{"target": "VPS jobs / sauce-watch.service", '
       '"outcome": "fixed_verified", "issue": "i", "action": "a"}]')
rep = hr.parse_results(txt, two)
check("reported target parsed", rep[two[0]["key"]]["outcome"] == "fixed_verified")
check("unreported target = unparsed", rep[two[1]["key"]]["outcome"] == "unparsed")
check("garbage = unparsed", hr.parse_results("no contract", [judge])[judge["key"]]["outcome"]
      == "unparsed")
single = hr.parse_results('HC_REPAIR_RESULT: [{"target": "typo", "outcome": "false_alarm"}]',
                          [judge])
check("single target tolerates a label typo", single[judge["key"]]["outcome"] == "false_alarm")
check("unknown outcome rejected",
      hr.parse_results('HC_REPAIR_RESULT: [{"target": "God judge", "outcome": "yay"}]',
                       [judge])[judge["key"]]["outcome"] == "unparsed")

# 6. Prompt guardrails.
p = hr.build_prompt(targets, now_et="2026-09-26 21:00 EDT", head="abc",
                    journals={"god-judge.timer": "journal line"})
for needle in ("/investigate", "NEVER `git push`", "telegram-forwarder", "claude-channels",
               "fake success ping", "hc-repair:", "HC_REPAIR_RESULT", "set_env_local.py",
               "### VPS jobs / sauce-watch.service", "### God judge", "journal line",
               "spawns agents"):
    check(f"prompt mentions {needle!r}", needle in p)
check("prompt starts with the skill", p.startswith("/investigate "))

# 7. Card renders every target.
card = hr.dm_card(two, rep, commits=[], status_after={"jobs-uuid": "up"}, meta={}, pushed=None)
check("card has both targets + check status",
      "sauce-watch.service" in card and "env-backup.path" in card and "up" in card)

# 8. Invoker swaps model/effort.
inv = hr.RepairInvoker("/bin/claude", oauth_token="t")
cmd = inv.command("x")
check("invoker uses repair model/effort",
      cmd[cmd.index("--model") + 1] == hr.REPAIR_MODEL
      and cmd[cmd.index("--effort") + 1] == hr.REPAIR_EFFORT)
check("invoker keeps --strict-mcp-config", "--strict-mcp-config" in cmd)

# 9. healthchecks API blip: retried, then skipped (None) — never a crash;
#    an HTTP error (revoked key) still raises.
import urllib.error  # noqa: E402
_real_get = hr.api_get
calls = []
def _flaky(path, raw=False):
    calls.append(path)
    if len(calls) < 2:
        raise urllib.error.URLError("_ssl.c:983: The handshake operation timed out")
    return {"checks": [{"name": "x"}]}
hr.api_get = _flaky
check("blip then success returns checks", hr.list_checks(sleep=lambda s: None) == [{"name": "x"}])
def _dead(path, raw=False):
    raise TimeoutError("timed out")
hr.api_get = _dead
check("persistent unreachable returns None", hr.list_checks(sleep=lambda s: None) is None)
def _401(path, raw=False):
    raise urllib.error.HTTPError(hr.API + path, 401, "Unauthorized", {}, None)
hr.api_get = _401
try:
    hr.list_checks(sleep=lambda s: None)
    check("HTTP 401 raises", False)
except urllib.error.HTTPError:
    check("HTTP 401 raises", True)
hr.api_get = _real_get

# 10. Agent-first notification invariant (operator decision 2026-10-11): a
#     job check tagged `agent-first` has no healthchecks channels — hc-repair
#     guarantees ONE DM per failure episode on every no-agent path.
af_checks = [
    {"name": "God judge", "uuid": "judge-uuid", "status": "down", "tags": "agent-first"},
    {"name": "VPS jobs", "uuid": "jobs-uuid", "status": "down", "tags": "vps infra"},
]
af_targets = hr.build_targets(af_checks, env=ENV, details=DETAILS, covered=COVERED)
af_judge = next(t for t in af_targets if t["check"] == "God judge")
af_unit = next(t for t in af_targets if ":" in t["key"])
check("tag marks a target agent-first", af_judge["agent_first"] is True)
check("untagged check is not agent-first", af_unit["agent_first"] is False)

stA = {"judge-uuid": {}, af_unit["key"]: {}}
skips = [(af_judge, "cooldown (1:00:00 < 6h)"), (af_unit, "cooldown (1:00:00 < 6h)")]
check("cooldown skip raw-alerts only agent-first targets",
      [t["key"] for t, _ in hr.skip_alerts(skips, stA)] == ["judge-uuid"])
check("debounce skip never raw-alerts",
      hr.skip_alerts([(af_judge, "debounce (down 0:04:00 < 0:10:00)")], stA) == [])
check("already-notified episode stays silent",
      hr.skip_alerts(skips, {"judge-uuid": {"notified": True}}) == [])
check("parked-with-DM-sent skip raw-alerts a new episode",
      hr.skip_alerts([(af_judge, "parked (needs_human)")], stA)[0][1] == "parked (needs_human)")

rtxt = hr.raw_alert_text(af_judge, "cooldown (1:00:00 < 6h)",
                         {"last_outcome": "transient_no_change"})
check("raw alert names check + reason + last outcome",
      "God judge" in rtxt and "cooldown" in rtxt and "transient_no_change" in rtxt)
check("raw alert carries the failure body tail",
      "boom tail" in hr.raw_alert_text({"check": "X", "key": "u", "body": "a\nboom tail"},
                                       "cooldown (x)", {}))
sh = hr.self_heal_text({"check": "MOE grade",
                        "first_seen": iso(NOW - timedelta(minutes=7))}, NOW)
check("self-heal note names check + minutes", "MOE grade" in sh and "~7 min" in sh)
check("self-heal notes a failed card DM",
      "card DM failed" in hr.self_heal_text(
          {"check": "x", "first_seen": iso(NOW), "attempts": 1}, NOW))

stB: dict = {}
hr.observe(stB, af_targets, NOW)
check("observe stamps agent_first", stB["judge-uuid"]["agent_first"] is True)
notify_calls: list = []
notify_ok = {"v": False}
def _notify(key, entry):
    notify_calls.append(key)
    return notify_ok["v"]
hr.observe(stB, [], NOW + timedelta(minutes=5), notify=_notify)
check("failed self-heal DM keeps the episode",
      notify_calls == ["judge-uuid"] and "first_seen" in stB.get("judge-uuid", {}))
notify_ok["v"] = True
hr.observe(stB, [], NOW + timedelta(minutes=10), notify=_notify)
check("successful self-heal DM ends the episode",
      notify_calls == ["judge-uuid", "judge-uuid"] and "judge-uuid" not in stB)
stC = {"k1": {"agent_first": True, "first_seen": iso(NOW), "notified": True, "check": "x"}}
quiet: list = []
hr.observe(stC, [], NOW, notify=lambda k, e: quiet.append(k) or True)
check("notified episode recovers silently", quiet == [] and "k1" not in stC)

pp = hr.build_prompt([af_judge], now_et="t", head="h", journals={})
check("prompt flags agent-first (operator not paged)", "NOT been paged" in pp)
pbox = hr.build_prompt([af_unit], now_et="t", head="h", journals={})
check("prompt notes box-level checks already paged", "already paged" in pbox)

print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
