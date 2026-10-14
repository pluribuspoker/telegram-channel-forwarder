"""Regression test: "Feyenoord / Como O2.5" is ONE match total, not a parlay.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_game_title_total.py

James Bets posted "Feyenoord / Como O2.5" (1910823870:582920, 2026-10-14) —
the Como at Feyenoord UCL match total, DraftKings o2.5 -165. It parsed as two
parlay legs ("Feyenoord game over 2.5", "Como game over 2.5") bound to the
same event, and _insert_odds multiplied -165 x -165 into a fake +158.
_merge_game_title_total (ai.py) re-joins the shape into one game total.
"""
import contextlib
import copy
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import _mark_slash_parlay_legs, _merge_game_title_total
from tracker_format import _insert_odds

INCIDENT_TEXT = "James Bets\n\nFeyenoord / Como O2.5"


def leg(team, line=2.5, direction="over", parlay=True, period="game"):
    return {"description": f"{team.split()[0]} game {direction} {line:g} goals",
            "sport": None, "bet_type": "total", "is_parlay_leg": parlay,
            "period": period, "teams": [team], "player": None,
            "prop_stat": None, "line": line, "direction": direction}


def run(text, picks):
    parsed = {"sport": "Soccer", "picks": copy.deepcopy(picks)}
    with contextlib.redirect_stdout(io.StringIO()):
        _merge_game_title_total(parsed, text)
        _mark_slash_parlay_legs(parsed, text)
    return parsed["picks"]


INCIDENT = [leg("Feyenoord"), leg("Como 1907")]
fails = 0


def check(name, ok):
    global fails
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    fails += not ok


out = run(INCIDENT_TEXT, INCIDENT)
check("incident → one pick", len(out) == 1)
check("incident → both teams", out[0]["teams"] == ["Feyenoord", "Como 1907"])
check("incident → standalone", out[0]["is_parlay_leg"] is False)
check("incident → game total over 2.5",
      (out[0]["bet_type"], out[0]["direction"], out[0]["line"]) == ("total", "over", 2.5))
check("incident → description names the match",
      out[0]["description"] == "Feyenoord vs Como 1907 game total over 2.5 goals")

odds = {"0": {"odds": -165, "match_type": "pregame_espn_exact"}}
tagged = _insert_odds(INCIDENT_TEXT, out, odds)
check("odds: single -165, no multiplied price", "-165" in tagged and "+158" not in tagged)

out = run(INCIDENT_TEXT, [leg("Feyenoord", parlay=False), leg("Como 1907", parlay=False)])
check("standalone split → one pick", len(out) == 1 and out[0]["is_parlay_leg"] is False)

out = run("Feyenoord / Como O2.5 (2u)", INCIDENT)
check("unit sizing on the line still merges", len(out) == 1)

out = run("Feyenoord O2.5 / Como O2.5", INCIDENT)
check("per-team lines stay a 2-leg parlay",
      len(out) == 2 and all(p["is_parlay_leg"] for p in out))

out = run(INCIDENT_TEXT, [leg("Feyenoord"), leg("Como 1907", direction="under")])
check("different directions untouched", len(out) == 2)

out = run("Feyenoord / Como O2.5", [leg("Feyenoord"), leg("Como 1907", line=3.5)])
check("different lines untouched", len(out) == 2)

out = run("> Feyenoord / Como O2.5", INCIDENT)
check("blockquote never the bet line", len(out) == 2)

tt = [dict(leg("Feyenoord"), bet_type="team_total"), dict(leg("Como 1907"), bet_type="team_total")]
out = run(INCIDENT_TEXT, tt)
check("team totals untouched", len(out) == 2)

three = INCIDENT + [{**leg("Ajax"), "bet_type": "moneyline", "line": None, "direction": None}]
out = run("Feyenoord / Como O2.5\nAjax ML", three)
check("merged total stays a leg beside another parlay leg",
      len(out) == 2 and out[0]["is_parlay_leg"] is True and out[0]["teams"] == ["Feyenoord", "Como 1907"])

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
