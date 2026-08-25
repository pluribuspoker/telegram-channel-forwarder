"""Regression: player props must see EVERY box-score stat group.

ESPN splits MLB boxes into batting/pitching (NFL: passing/rushing/…, NHL:
forwards/defenses/goalies). box_score_text used to read only the first group
with a curated batting/basketball key map, so every pitcher prop rendered
"No player stats found" and graded UNKNOWN until the cap froze it
(Trent msgs 69/70: Skubal 11 K WIN and Snell 6 K LOSS both stuck ungraded).

Also pins the stale-roster rescue: a player traded after the model's training
cutoff gets parsed onto his OLD team, binding the wrong game ("Skubal getting
9" → Detroit Tigers; he'd been traded to the Dodgers). build_context must
scan the rest of the day's completed slate for him, and the tracker's
_player_team_unevidenced must send slip-attached parses like that to the
image parse.

Fixtures are pruned REAL ESPN summary payloads from the incident (2026-08-22/23).
Offline — no network, no API spend.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scores import box_score_text  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SNELL = json.load(open(FIXTURES / "espn_mlb_summary_snell_20260823.json"))
SKUBAL = json.load(open(FIXTURES / "espn_mlb_summary_skubal_20260822.json"))
DET_KC = json.load(open(FIXTURES / "espn_mlb_summary_det_kc_20260822.json"))

passed = failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


# ── pitching group visible ────────────────────────────────────────────────────
snell_line = box_score_text(SNELL, "Blake Snell")
check("Snell found in pitching group", "Blake Snell" in snell_line, repr(snell_line))
check("Snell strikeouts visible", "strikeouts=6" in snell_line, repr(snell_line))
check("Snell line labeled pitching", "pitching" in snell_line, repr(snell_line))

skubal_line = box_score_text(SKUBAL, "Tarik Skubal")
check("Skubal found in pitching group", "Tarik Skubal" in skubal_line, repr(skubal_line))
check("Skubal strikeouts visible", "strikeouts=11" in skubal_line, repr(skubal_line))

# The wrong game genuinely lacks the player — must still say so.
check("absent player still reports not found",
      box_score_text(DET_KC, "Tarik Skubal") == "No player stats found")

# ── batting group still visible (regression guard for the old behavior) ──────
full = box_score_text(SNELL)
check("batting group still emitted", "batting" in full, full[:200])
check("batting stats still emitted", "atBats=" in full, full[:200])
check("pitching group in full dump", ", pitching)" in full, full[:200])

# ── strict all-words matching for cross-slate scans ──────────────────────────
# Real collision from the fixture: "Nick Gonzalez" loosely matches BOTH
# Nick Gonzales (via "nick") and Jacob Gonzalez (via "gonzalez"); the strict
# all-words match used for cross-slate scans must match neither.
loose = box_score_text(SNELL, "Nick Gonzalez")
check("loose match hits name collisions",
      "Nick Gonzales" in loose and "Jacob Gonzalez" in loose, repr(loose))
strict = box_score_text(SNELL, "Nick Gonzalez", require_all_words=True)
check("strict match rejects collisions", strict == "No player stats found", repr(strict))
strict_ok = box_score_text(SNELL, "Blake Snell", require_all_words=True)
check("strict match still finds the real player",
      "strikeouts=6" in strict_ok, repr(strict_ok))

# ── build_context stale-roster rescue ────────────────────────────────────────
import ai  # noqa: E402

_events = [
    {"id": "det", "name": "Detroit Tigers at Kansas City Royals",
     "status": {"type": {"completed": True}},
     "competitions": [{"competitors": [
         {"team": {"displayName": "Detroit Tigers"}},
         {"team": {"displayName": "Kansas City Royals"}}]}]},
    {"id": "lad", "name": "Pittsburgh Pirates at Los Angeles Dodgers",
     "status": {"type": {"completed": True}},
     "competitions": [{"competitors": [
         {"team": {"displayName": "Pittsburgh Pirates"}},
         {"team": {"displayName": "Los Angeles Dodgers"}}]}]},
]
_scoreboard = {"events": _events}
_summaries = {"det": DET_KC, "lad": SKUBAL}


async def _fake_fetch_summary(sport, eid):
    return _summaries[eid]


ai.fetch_espn_summary = _fake_fetch_summary

_pick = {
    "description": "Tarik Skubal over 8.5 Pitcher Ks", "bet_type": "prop",
    "period": "game", "teams": ["Detroit Tigers"],  # stale roster memory
    "player": "Tarik Skubal", "prop_stat": "K", "line": 8.5, "direction": "over",
}
ctx, gd = asyncio.run(ai.build_context("MLB", "2026-08-22", _pick, _scoreboard, {}))
check("rescue found traded player's real game", "strikeouts=11" in ctx, ctx[:300])
check("rescue context carries the mismatch note", "NOTE:" in ctx, ctx[:300])

# Correct team binds normally — no rescue note, right game.
_pick_ok = dict(_pick, teams=["Los Angeles Dodgers"])
ctx2, _ = asyncio.run(ai.build_context("MLB", "2026-08-22", _pick_ok, _scoreboard, {}))
check("correct team needs no rescue", "NOTE:" not in ctx2, ctx2[:300])
check("correct team sees the player", "strikeouts=11" in ctx2, ctx2[:300])
check("prop context includes line scores", "Final=" in ctx2, ctx2[:300])

# ── tracker trigger: team unevidenced in text → consult slip image ───────────
from tracker import _player_team_unevidenced  # noqa: E402

_parsed = {"sport": "MLB", "picks": [_pick]}
check("bare player text triggers image consult",
      _player_team_unevidenced(_parsed, "Life on line. Skubal getting 9."))
check("team named in text needs no image",
      not _player_team_unevidenced(_parsed, "Tigers' Skubal getting 9."))
check("non-player picks never trigger",
      not _player_team_unevidenced(
          {"picks": [{"description": "Tigers ML", "teams": ["Detroit Tigers"]}]},
          "hammer this ML"))
check("no-teams player pick left to _parse_incomplete",
      not _player_team_unevidenced(
          {"picks": [{"player": "Tarik Skubal", "teams": []}]},
          "Skubal getting 9"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
