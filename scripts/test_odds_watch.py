#!/usr/bin/env python3
"""Offline tests for the odds watch (scripts/odds_watch.py).

Recall on the real wrong-odds classes the operator reported by hand (each
fixture is the pre-fix shape: the cache now holds the repaired entries), the
false-positive filters (a capper-stated price, live prices, main lines), the
per-instance gate (legit stays quiet until the price changes), the result
contract and the silent-when-legit card. No network, no claude binary, no DMs.

    python scripts/test_odds_watch.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import odds_watch as ow  # noqa: E402
import odds_checks as oc  # noqa: E402
import odds_gate as og  # noqa: E402
from tracker_format import _insert_odds  # noqa: E402

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def pick(desc, bt, *, teams=None, leg=False, period="game", line=None):
    return {"description": desc, "sport": "NFL", "bet_type": bt, "is_parlay_leg": leg,
            "period": period, "teams": teams or [], "player": None, "prop_stat": None,
            "line": line, "direction": None}


def o(price, mt="exact", **kw):
    return {"odds": price, "match_type": mt, "bookmaker": "espn_draftkings", **kw}


def rules(entry, key="-100:1"):
    return sorted({h["rule"] for h in oc.scan_entry(key, entry)})


# ── recall: the hand-reported classes ────────────────────────────────────────
VT = {  # 2026-10-02 Trent 133: same-side spread & ML parsed as a 2-leg parlay
    "html_text": "VIRGINIA TECH (5U)\n\n-2.5 SPREAD &amp; ML 🔒 [+245]",
    "parsed": {"picks": [
        pick("Virginia Tech Hokies -2.5 (parlay leg)", "spread", teams=["Virginia Tech Hokies"], leg=True, line=-2.5),
        pick("Virginia Tech Hokies ML (parlay leg)", "moneyline", teams=["Virginia Tech Hokies"], leg=True)]},
    "odds_by_pick": {"0": o(-105), "1": o(-130)},
}
check("VT spread&ML parlay → parlay_same_game", rules(VT) == ["parlay_same_game"], rules(VT))
vt_hit = oc.scan_entry("-100:1", VT)[0]
check("same-game hit carries the multiplied price", vt_hit["price"] == 245, vt_hit)

TT = {  # bare "Chiefs over 42.5" misread as a team total → deep alternate
    "html_text": "Chiefs over 42.5 [+3500]",
    "parsed": {"picks": [pick("Kansas City Chiefs team total over 42.5", "team_total",
                              teams=["Kansas City Chiefs"], line=42.5)]},
    "odds_by_pick": {"0": o(3500)},
}
check("team-total misread +3500 → band", rules(TT) == ["band"], rules(TT))

TEASER = {  # teaser legs priced at the pre-tease main lines
    "html_text": "7 pt teaser: [+264]\nEagles PK\nRams +0.5",
    "parsed": {"picks": [
        pick("Eagles PK teaser leg", "spread", teams=["Philadelphia Eagles"], leg=True, line=0),
        pick("Rams +0.5 teaser leg", "spread", teams=["Los Angeles Rams"], leg=True, line=0.5)]},
    "odds_by_pick": {"0": o(-110), "1": o(-110)},
}
check("teaser at main-line prices → teaser_long", rules(TEASER) == ["teaser_long"], rules(TEASER))
TEASER_OK = {**TEASER, "odds_by_pick": {"0": o(-250), "1": o(-240)}}
check("teaser at teased-line prices is quiet (leg band exempt)", rules(TEASER_OK) == [], rules(TEASER_OK))

WRONG_GAME = {
    "html_text": "Bulldogs +6.5 [-110]",
    "parsed": {"picks": [pick("Mississippi State +6.5", "spread", teams=["Mississippi State Bulldogs"], line=6.5)]},
    "odds_by_pick": {"0": o(-110, commence_time="2026-09-19T23:45Z")},
    "espn_events": {"0": {"status": "bound", "kickoff": "2026-09-26T23:45Z",
                          "name": "Missouri Tigers at Mississippi State Bulldogs"}},
}
check("price from last week's game → game_mismatch", rules(WRONG_GAME) == ["game_mismatch"], rules(WRONG_GAME))
SAME_GAME = {**WRONG_GAME, "odds_by_pick": {"0": o(-110, commence_time="2026-09-26T23:40Z")}}
check("same kickoff (minutes apart) is quiet", rules(SAME_GAME) == [], rules(SAME_GAME))

NOW = {"html_text": "Bills -3 (-110) [-190 now]",
       "parsed": {"picks": [pick("Bills -3 (-110)", "spread", teams=["Buffalo Bills"], line=-3)]},
       "odds_by_pick": {"0": o(-190)}}
check("stated -110 vs ours -190 → now_move", "now_move" in rules(NOW), rules(NOW))
NOW_SMALL = {**NOW, "html_text": "Bills -3 (-110) [-125 now]", "odds_by_pick": {"0": o(-125)}}
check("a small move stays quiet", rules(NOW_SMALL) == [], rules(NOW_SMALL))

PROX = {"html_text": "Broncos +7.5 [-169]",
        "parsed": {"picks": [pick("Broncos +7.5", "spread", teams=["Denver Broncos"], line=7.5)]},
        "odds_by_pick": {"0": o(-169, "proximity_2.5pts")}}
check("estimate 2.5 pts off → proximity_gap", rules(PROX) == ["proximity_gap"], rules(PROX))
PROX_NEAR = {**PROX, "odds_by_pick": {"0": o(-120, "proximity_0.5pts")}}
check("half-point estimate is quiet", rules(PROX_NEAR) == [], rules(PROX_NEAR))

HIDDEN = {"html_text": "Michigan -3 -160 1U",  # capper's price shown, ours never displayed
          "parsed": {"picks": [pick("Michigan -3", "spread", teams=["Michigan Wolverines"], line=-3)]},
          "odds_by_pick": {"0": o(-158, "proximity_2.5pts")}}
check("an undisplayed straight price is quiet", rules(HIDDEN) == [], rules(HIDDEN))

# ── false-positive filters ──────────────────────────────────────────────────
ELKINS = {  # real cache entry shape: capper stated +500, we priced +561
    "html_text": "Darren Elkins ML (+500) 1u",
    "parsed": {"picks": [pick("Darren Elkins ML (+500)", "moneyline", teams=["Darren Elkins"])]},
    "odds_by_pick": {"0": o(561)},
}
check("long ML corroborated by the capper's price is quiet", rules(ELKINS) == [], rules(ELKINS))
ELKINS_UNSTATED = {**ELKINS, "html_text": "Darren Elkins ML 1u [+561]", "parsed": {"picks": [pick("Darren Elkins ML", "moneyline")]}}
check("the same long ML unstated → ml_long", rules(ELKINS_UNSTATED) == ["ml_long"], rules(ELKINS_UNSTATED))
LIVE = {"html_text": "Jets +14.5 [+1200 live]",
        "parsed": {"picks": [pick("Jets +14.5", "spread", teams=["New York Jets"], line=14.5)]},
        "odds_by_pick": {"0": o(1200, "live_exact")}}
check("live prices are exempt", rules(LIVE) == [], rules(LIVE))
MAIN = {"html_text": "Broncos +3.5 [-190]",
        "parsed": {"picks": [pick("Broncos +3.5", "spread", teams=["Denver Broncos"], line=3.5)]},
        "odds_by_pick": {"0": o(-190)}}
check("a priced alternate within the band is quiet", rules(MAIN) == [], rules(MAIN))
TWO_GAMES = {"html_text": "Parlay: [+264]\nEagles ML\nRavens ML",
             "parsed": {"picks": [pick("Eagles ML", "moneyline", teams=["Philadelphia Eagles"], leg=True),
                                  pick("Ravens ML", "moneyline", teams=["Baltimore Ravens"], leg=True)]},
             "odds_by_pick": {"0": o(-325), "1": o(-375)}}
check("ordinary 2-game ML parlay is quiet", rules(TWO_GAMES) == [], rules(TWO_GAMES))

# ── fan-out ─────────────────────────────────────────────────────────────────
def fan(price):
    return {"capper_name": "Trent", "html_text": "x",
            "parsed": {"picks": [pick("Bills -3", "spread", teams=["Buffalo Bills"], line=-3)]},
            "odds_by_pick": {"0": o(price, game_date="2026-10-04")}}
cache = {"-100:1": fan(-110), "-200:1": fan(-180)}
fh = oc.scan_fanout(cache, list(cache))
check("fan-out copies -110 vs -180 → fanout_price on both",
      sorted(h["key"] for h in fh) == ["-100:1", "-200:1"], fh)
cache = {"-100:1": fan(-110), "-200:1": fan(-112)}
check("fan-out drift of 2 cents is quiet", oc.scan_fanout(cache, list(cache)) == [])
one = {"capper_name": "T", "html_text": "x",
       "parsed": {"picks": [pick("Same", "spread", line=1), pick("Same", "spread", line=1)]},
       "odds_by_pick": {"0": o(-110, game_date="d"), "1": o(150, game_date="d")}}
check("two legs of ONE message are never a fan-out split",
      oc.scan_fanout({"-1:1": one}, ["-1:1"]) == [])

# ── gate / state ────────────────────────────────────────────────────────────
NOW_T = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
h = {"key": "-100:1", "idx": 0, "rule": "band", "price": 3500, "detail": ""}
state: dict = {}
check("fresh flag runs", ow.gate(state, h, NOW_T)[0] == "run")
ow.record_spawn(state, [h], NOW_T)
ow.settle(state, h, "legit", NOW_T)
check("judged legit → quiet at the same price", ow.gate(state, h, NOW_T)[0] == "skip")
check("…but a CHANGED price is a new question", ow.gate(state, {**h, "price": 3000}, NOW_T)[0] == "run")
ow.record_spawn(state, [{**h, "price": 3000}], NOW_T)
check("a re-priced instance restarts its attempt count",
      state[ow.instance_key(h)]["attempts"] == 1, state)
state = {}
for _ in range(ow.ATTEMPT_CAP):
    ow.record_spawn(state, [h], NOW_T)
    ow.settle(state, h, "unparsed", NOW_T)
check("attempt cap parks a repeatedly unparsed flag", ow.gate(state, h, NOW_T)[0] == "skip", state)
state = {}
ow.record_spawn(state, [h], NOW_T)
ow.settle(state, h, "needs_human", NOW_T)
check("needs_human parks", state[ow.instance_key(h)].get("parked") is True)
state = {"_spawns": [NOW_T.isoformat()] * 3 + ["2026-10-01T00:00:00+00:00"]}
check("daily cap counts the rolling 24h only", ow.daily_spawns(state, NOW_T) == 3)

# ── grouping, result contract, card ─────────────────────────────────────────
cache = {"-100:1": {**VT, "_source_key": "-9:5", "msg_date": "2026-10-03T10:00:00+00:00", "capper_name": "Trent"},
         "-200:1": {**VT, "_source_key": "-9:5", "msg_date": "2026-10-03T10:00:01+00:00", "capper_name": "Trent"},
         "-300:1": {**TT, "msg_date": "2026-10-03T11:00:00+00:00", "capper_name": "X"}}
groups = ow.group_by_message(ow.scan(cache, list(cache)), cache)
check("fan-out copies group under their source, newest first",
      [g["source"] for g in groups] == ["-300:1", "-9:5"] and groups[1]["keys"] == ["-100:1", "-200:1"],
      [(g["source"], g["keys"]) for g in groups])
result = ('done\nODDS_WATCH_RESULT: [{"message": "-9:5", "outcome": "fixed", "issue": "i", "action": "a"},'
          ' {"message": "-300:1", "outcome": "legit", "issue": "real alt", "action": "none"}]')
rep = ow.parse_results(result, groups)
check("result contract parses per source", rep["-9:5"]["outcome"] == "fixed"
      and rep["-300:1"]["outcome"] == "legit", rep)
check("an unreported message is unparsed",
      ow.parse_results("no contract", groups)["-9:5"]["outcome"] == "unparsed")
check("all-legit pass sends no card", ow.dm_card(groups, {g["source"]: {"outcome": "legit"} for g in groups},
                                                 commits=[], notes=[], meta={}) is None)
card = ow.dm_card(groups, rep, commits=["abc odds-watch: x"], notes=[], meta={})
check("card shows the fix, counts the legit flag, links the post",
      card and "fixed" in card and "1 other flag(s) judged legit" in card and "href=\"https://t.me/c/" in card, card)
prompt = ow.build_prompt(groups, cache, now_et="2026-10-03 08:00 EDT", head="abc")
check("prompt: judge first, legit is normal, never push, result contract",
      all(s in prompt for s in ("JUDGE", "legit", "NEVER `git push`", "ODDS_WATCH_RESULT", "-9:5",
                                "odds-watch:")), prompt[:300])
cmd = ow.WatchInvoker("claude", oauth_token="t").command("p")
check("invoker runs the watch model/effort",
      cmd[cmd.index("--model") + 1] == ow.MODEL and cmd[cmd.index("--effort") + 1] == ow.EFFORT)


# ── misses ──────────────────────────────────────────────────────────────────
def miss_entry(mt, **o_extra):
    return {"html_text": "x", "parsed": {"picks": [pick("Italy vs Belgium BTTS", "prop")]},
            "odds_by_pick": {"0": {"odds": None, "match_type": mt, **o_extra}}}
mh = oc.scan_entry("-1:1", miss_entry("prop_stat_unsupported(BTTS)"), now=NOW_T)
check("unsupported market miss → miss with a class",
      [h["rule"] for h in mh] == ["miss"] and mh[0]["miss_class"] == "prop_stat_unsupported(BTTS)", mh)
far = miss_entry("no_spread_data", commence_time="2026-10-04T00:00Z", _retry_n=1)
check("retryable miss with kickoff far away waits for the tracker's retries",
      oc.scan_entry("-1:1", far, now=NOW_T) == [])
near = miss_entry("no_spread_data", commence_time="2026-10-03T14:00Z")
nh = oc.scan_entry("-1:1", near, now=NOW_T)
check("retryable miss inside 3h of kickoff → per-game miss (no class)",
      [h["rule"] for h in nh] == ["miss"] and nh[0]["miss_class"] is None, nh)
check("no_game with no kickoff waits for two failed retries",
      oc.scan_entry("-1:1", miss_entry("no_game", _retry_n=1), now=NOW_T) == []
      and len(oc.scan_entry("-1:1", miss_entry("no_game", _retry_n=2), now=NOW_T)) == 1)
check("game_in_progress misses are never flagged",
      oc.scan_entry("-1:1", miss_entry("game_in_progress"), now=NOW_T) == [])
state = {}
ow.record_spawn(state, mh, NOW_T)
ow.settle(state, mh[0], "no_free_source", NOW_T)
other = {**mh[0], "key": "-2:9"}
check("no_free_source parks the whole miss CLASS (another pick of it skips)",
      ow.gate(state, other, NOW_T)[0] == "skip", state)
check("…for CLASS_PARK_DAYS only",
      ow.gate(state, other, NOW_T + ow.timedelta(days=ow.CLASS_PARK_DAYS + 1))[0] == "run")
check("--rearm-style class removal: a per-game miss never parks a class",
      ow.class_key({**nh[0]}) is None)

# ── holds ───────────────────────────────────────────────────────────────────
HELD = {"html_text": "Chiefs over 42.5", "parsed": TT["parsed"],
        "odds_by_pick": {"0": {**o(3500), "hold": {"why": ["band: +3500"], "at": "2026-10-03T11:30:00+00:00"}}}}
hh = oc.scan_entry("-1:1", HELD, now=NOW_T)
check("a held leg is ONE hold flag carrying the gate's reasons (no double band)",
      [h["rule"] for h in hh] == ["hold"] and "band: +3500" in hh[0]["detail"], hh)
check("_insert_odds shows no tag for a held leg",
      "[+3500]" not in _insert_odds("Chiefs over 42.5", TT["parsed"]["picks"], HELD["odds_by_pick"]))
check("…and the tag returns once released",
      "[+3500]" in _insert_odds("Chiefs over 42.5", TT["parsed"]["picks"], {"0": o(3500)}))
vt_held = {"0": o(-105), "1": {**o(-130), "hold": {"why": ["x"], "at": "t"}}}
check("one held parlay leg hides the combined ticket tag",
      "[+245]" not in _insert_odds("VT\n-2.5 SPREAD & ML parlay:", VT["parsed"]["picks"], vt_held))
h0 = {**hh[0], "hold_at": "2026-10-03T11:55:00+00:00"}
check("a fresh hold the agent is about to judge is not released",
      ow.release_due([h0], {}, NOW_T, agent_keys={ow.instance_key(h0)}) == [])
check("a fresh hold outside this batch waits (not stale yet)",
      ow.release_due([h0], {}, NOW_T, agent_keys=set()) == [])
stale = {**h0, "hold_at": "2026-10-03T09:00:00+00:00"}
check("a hold past HOLD_MAX_MINUTES with no agent is released (fail open)",
      ow.release_due([stale], {}, NOW_T, agent_keys=set()) == [stale])
st_parked = {ow.instance_key(h0): {"parked": True, "parked_reason": "attempt cap (2)"}}
check("a hold no agent will ever judge (parked) is released at once",
      ow.release_due([h0], st_parked, NOW_T, agent_keys=set()) == [h0])
st_nh = {ow.instance_key(stale): {"parked": True, "parked_reason": "needs_human"}}
check("a needs_human hold stays hidden even when stale",
      ow.release_due([stale], st_nh, NOW_T, agent_keys=set()) == [])
grp = ow.group_by_message(hh + oc.scan_entry("-3:1", TT, now=NOW_T),
                          {"-1:1": {"msg_date": "2026-10-02"}, "-3:1": {"msg_date": "2026-10-03"}})
check("held messages go to the agent first", grp[0]["source"] == "-1:1", [g["source"] for g in grp])

# ── the pre-publish gate ────────────────────────────────────────────────────
og.REVIEW_CACHE = Path(__import__("tempfile").mkdtemp()) / "rc.json"
og.REVIEW_LOG = og.REVIEW_CACHE.with_name("log.jsonl")
calls = []
def fake_reviewer(answer):
    def _r(msg):
        calls.append(msg)
        return answer, {"fake": True}
    return _r
def gate_on(entry, fresh, answer, key="-5:1", **kw):
    odds = {k: dict(v) for k, v in entry["odds_by_pick"].items()}
    picks = entry["parsed"]["picks"]
    tagged = _insert_odds(entry["html_text"], picks, odds)
    r = og.gate(key, entry, picks, odds, raw_text=entry["html_text"], tagged_html=tagged,
                fresh=fresh, now=NOW_T, reviewer=fake_reviewer(answer), **kw)
    return r, odds
r, odds = gate_on(VT, {0, 1}, [])
check("gate: free check alone holds both same-game parlay legs + triggers",
      sorted(r["held"]) == [0, 1] and r["trigger"] and odds["0"].get("hold"), r["held"])
F5 = {"html_text": "Yankees F5 ML", "parsed": {"picks": [pick("Yankees F5 ML", "moneyline",
       teams=["New York Yankees"], period="1h")]}, "odds_by_pick": {"0": o(-150)}}
r, odds = gate_on(F5, {0}, [{"leg": 0, "reason": "priced like the full-game ML"}], key="-6:1")
check("gate: a Claude-only suspicion holds the leg (normal-looking price)",
      list(r["held"]) == [0] and "review: priced like the full-game ML" in odds["0"]["hold"]["why"], r)
n = len(calls)
r2, _ = gate_on(F5, {0}, [], key="-6:1")
check("gate: a fan-out copy at the same prices reuses the source's verdict (one call)",
      len(calls) == n and list(r2["held"]) == [0], (len(calls), n, r2["held"]))
r, odds = gate_on(MAIN, {0}, [])
check("gate: a clean price holds nothing and triggers nothing",
      r["held"] == {} and not r["trigger"] and "hold" not in odds["0"])
r, odds = gate_on(F5, {0}, None, key="-7:1")
check("gate: a failed review fails OPEN (nothing held)", r["held"] == {} and not r["trigger"], r)
r, odds = gate_on(TT, set(), [{"leg": 0, "reason": "x"}], key="-8:1")
check("gate: legs not priced this pass are never re-held (no review call either)",
      r["held"] == {} and not r["review"]["ran"])
r, _ = gate_on(miss_entry("prop_stat_unsupported(BTTS)"), {0}, [], key="-9:1")
check("gate: a fresh unpriced leg triggers the agent (find a free source)",
      r["trigger"] and r["held"] == {}, r)
r, _ = gate_on(F5, {0}, [{"leg": 0, "reason": "x"}], key="-10:1", review_enabled=False)
check("gate: ODDS_REVIEW_DISABLED skips the Claude review", not r["review"]["ran"] and r["held"] == {})
check("review parser takes ticket + leg suspects, rejects garbage",
      og.parse_review('ok {"suspect": [{"leg": "ticket", "reason": "r"}, {"leg": 2, "reason": "s"}]}')
      == [{"leg": "ticket", "reason": "r"}, {"leg": 2, "reason": "s"}]
      and og.parse_review("no json") is None and og.parse_review('{"suspect": "x"}') is None)
seen = {}
class _Done:
    returncode, stderr = 0, ""
    stdout = '{"result": "{\\"suspect\\": []}", "is_error": false}'
def _fake_run(cmd, **kw):
    seen.update(cmd=cmd, env=kw["env"], input=kw["input"])
    return _Done()
_real_run = og.subprocess.run
og.subprocess.run = _fake_run
import os as _os
_os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "tok"
_os.environ["ANTHROPIC_API_KEY"] = "sk-should-never-be-passed"
sus, meta = og.claude_review("msg")
og.subprocess.run = _real_run
check("review call is subscription-only: headless claude -p, OAuth env, no API key",
      sus == [] and seen["cmd"][:2] == [og.CLAUDE_BIN, "-p"] and "--safe-mode" in seen["cmd"]
      and seen["env"].get("CLAUDE_CODE_OAUTH_TOKEN") == "tok"
      and not any("ANTHROPIC" in k for k in seen["env"]), seen)

print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
