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
    return sorted({h["rule"] for h in ow.scan_entry(key, entry)})


# ── recall: the hand-reported classes ────────────────────────────────────────
VT = {  # 2026-10-02 Trent 133: same-side spread & ML parsed as a 2-leg parlay
    "html_text": "VIRGINIA TECH (5U)\n\n-2.5 SPREAD &amp; ML 🔒 [+245]",
    "parsed": {"picks": [
        pick("Virginia Tech Hokies -2.5 (parlay leg)", "spread", teams=["Virginia Tech Hokies"], leg=True, line=-2.5),
        pick("Virginia Tech Hokies ML (parlay leg)", "moneyline", teams=["Virginia Tech Hokies"], leg=True)]},
    "odds_by_pick": {"0": o(-105), "1": o(-130)},
}
check("VT spread&ML parlay → parlay_same_game", rules(VT) == ["parlay_same_game"], rules(VT))
vt_hit = ow.scan_entry("-100:1", VT)[0]
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
fh = ow.scan_fanout(cache, list(cache))
check("fan-out copies -110 vs -180 → fanout_price on both",
      sorted(h["key"] for h in fh) == ["-100:1", "-200:1"], fh)
cache = {"-100:1": fan(-110), "-200:1": fan(-112)}
check("fan-out drift of 2 cents is quiet", ow.scan_fanout(cache, list(cache)) == [])
one = {"capper_name": "T", "html_text": "x",
       "parsed": {"picks": [pick("Same", "spread", line=1), pick("Same", "spread", line=1)]},
       "odds_by_pick": {"0": o(-110, game_date="d"), "1": o(150, game_date="d")}}
check("two legs of ONE message are never a fan-out split",
      ow.scan_fanout({"-1:1": one}, ["-1:1"]) == [])

# ── gate / state ────────────────────────────────────────────────────────────
NOW_T = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
h = {"key": "-100:1", "idx": 0, "rule": "band", "price": 3500, "detail": ""}
state: dict = {}
check("fresh flag runs", ow.gate(state, h)[0] == "run")
ow.record_spawn(state, [h], NOW_T)
ow.settle(state, h, "legit")
check("judged legit → quiet at the same price", ow.gate(state, h)[0] == "skip")
check("…but a CHANGED price is a new question", ow.gate(state, {**h, "price": 3000})[0] == "run")
ow.record_spawn(state, [{**h, "price": 3000}], NOW_T)
check("a re-priced instance restarts its attempt count",
      state[ow.instance_key(h)]["attempts"] == 1, state)
state = {}
for _ in range(ow.ATTEMPT_CAP):
    ow.record_spawn(state, [h], NOW_T)
    ow.settle(state, h, "unparsed")
check("attempt cap parks a repeatedly unparsed flag", ow.gate(state, h)[0] == "skip", state)
state = {}
ow.record_spawn(state, [h], NOW_T)
ow.settle(state, h, "needs_human")
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

print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
