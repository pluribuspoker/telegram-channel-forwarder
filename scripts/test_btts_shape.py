"""Regression test: BTTS parsed as a game total converges onto the prop shape.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_btts_shape.py

James Bets "Italy / Belgium BTTS" (2026-09-25) parsed as bet_type=total,
line=0.5, over: the broadcast read "Italy/Belgium O0.5" and odds routed to a
game-total market (a 2-0 result wins O0.5 while BTTS loses). The verdict held
only because the grader read the description. _fix_btts_total_shape rewrites
the parse to prop/BTTS; the renderer (test_period_tag.py) covers old caches.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import _fix_btts_total_shape
from audit import _format_pick


def P(desc, bet_type="total", line=0.5, direction="over", prop_stat=None, player=None):
    return {"description": desc, "sport": "Soccer", "bet_type": bet_type,
            "is_parlay_leg": False, "period": "game", "teams": ["Italy", "Belgium"],
            "player": player, "prop_stat": prop_stat, "line": line,
            "direction": direction}


CASES = [
    # (name, pick, expected (bet_type, prop_stat, line), expected label)
    ("the incident: total o0.5", P("Italy vs Belgium - Both Teams to Score (BTTS)"),
     ("prop", "BTTS", None), "Italy vs Belgium BTTS Yes"),
    ("line-less total", P("Angers vs Rennes BTTS", line=None),
     ("prop", "BTTS", None), "Italy vs Belgium BTTS Yes"),
    ("No side", P("Italy vs Belgium BTTS No", line=None, direction="under"),
     ("prop", "BTTS", None), "Italy vs Belgium BTTS No"),
    ("already canonical", P("Italy vs Belgium BTTS", bet_type="prop", line=None, prop_stat="BTTS"),
     ("prop", "BTTS", None), "Italy vs Belgium BTTS Yes"),
    ("combo keeps its total", P("Italy vs Belgium BTTS & Over 2.5", line=2.5),
     ("total", None, 2.5), "Italy/Belgium O2.5"),
    ("plain o0.5 total untouched", P("Italy vs Belgium over 0.5 goals"),
     ("total", None, 0.5), "Italy/Belgium O0.5"),
]

fails = 0
for name, pick, shape, label in CASES:
    parsed = {"sport": "Soccer", "picks": [pick]}
    _fix_btts_total_shape(parsed)
    got = (pick["bet_type"], pick["prop_stat"], pick["line"])
    got_label = _format_pick(pick)
    ok = got == shape and got_label == label
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got} {got_label!r}")
print(f"\n{len(CASES) - fails}/{len(CASES)} passed")
sys.exit(1 if fails else 0)
