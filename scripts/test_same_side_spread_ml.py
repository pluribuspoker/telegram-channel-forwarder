"""Regression test: "TEAM -2.5 SPREAD & ML" is two straight bets, not a parlay.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_same_side_spread_ml.py

Trent posted "VIRGINIA TECH (5U) / -2.5 SPREAD & ML" (-1004394797084:133,
2026-10-02). _PARSE_PROMPT's "X & Y on one line = parlay" rule made it a
spread leg + ML leg on one ticket, and _insert_odds multiplied the legs as if
independent: -105 x -130 = [+245]. A covered -2.5 is a win, so no book prices
that pairing near the product — the slip attached was a straight ML.
_split_same_side_spread_ml (ai.py) re-reads the shape as two straights.

The same post also showed a literal "&amp;": X's rawContent is already
entity-escaped and trent_watcher.send_pick escaped it again.
"""
import asyncio
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import _split_same_side_spread_ml
from tracker_format import _insert_emojis, _insert_odds

VT = ["Virginia Tech Hokies"]


def leg(bet_type, teams=VT, line=None, period="game"):
    return {"description": f"{teams[0]} {bet_type}", "bet_type": bet_type,
            "is_parlay_leg": True, "period": period, "teams": list(teams),
            "player": None, "prop_stat": None, "line": line, "direction": None}


INCIDENT_TEXT = ("◼️ Trent\n\nFRIDAY NIGHT MORTAL MEGA MAX: \n\n"
                 "VIRGINIA TECH (5U) 🐳💣☢️\n\n-2.5 SPREAD &amp; ML 🔒\n\n"
                 "BOOTS ON GROUND IN BLACKSBURG. \n\nLETS HAVE A NIGHT. 🫡\n\n"
                 '<a href="https://x.com/BookitWithTrent/status/2106048481919996116">'
                 "🔗 View on X</a>")
RAW_TEXT = INCIDENT_TEXT.replace("&amp;", "&")

CASES = [
    ("the incident: same team, spread & ML, no ticket wording",
     [leg("spread", line=-2.5), leg("moneyline")], RAW_TEXT, [False, False]),
    ("explicit SGP wording keeps the parlay",
     [leg("spread", line=-2.5), leg("moneyline")], "VT -2.5 & ML SGP", [True, True]),
    ("'parlay' wording keeps the parlay",
     [leg("spread", line=-2.5), leg("moneyline")], "VT -2.5 + ML parlay", [True, True]),
    ("'2 leg' wording keeps the parlay",
     [leg("spread", line=-2.5), leg("moneyline")], "2-leg: VT -2.5 & ML", [True, True]),
    ("different teams stay a parlay",
     [leg("spread", line=-2.5), leg("moneyline", teams=["Pittsburgh Panthers"])],
     "VT -2.5 & Pitt ML", [True, True]),
    ("a third leg stays a parlay",
     [leg("spread", line=-2.5), leg("moneyline"), leg("moneyline", teams=["Alabama"])],
     "VT -2.5 & ML & Bama ML", [True, True, True]),
    ("spread + total is a real combo, untouched",
     [leg("spread", line=-2.5), leg("total", line=54.5)], "VT -2.5 & over 54.5",
     [True, True]),
    ("different periods stay a parlay",
     [leg("spread", line=-0.5, period="1h"), leg("moneyline")], "VT 1H -0.5 & ML",
     [True, True]),
]

failures = 0
for name, picks, text, expected in CASES:
    parsed = {"sport": "NCAAF", "picks": picks}
    with contextlib.redirect_stdout(io.StringIO()):
        _split_same_side_spread_ml(parsed, text)
    got = [p["is_parlay_leg"] for p in parsed["picks"]]
    ok = got == expected
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got}")

# End to end on the incident: no multiplied price, both straight prices shown.
parsed = {"sport": "NCAAF", "picks": [leg("spread", line=-2.5), leg("moneyline")]}
with contextlib.redirect_stdout(io.StringIO()):
    _split_same_side_spread_ml(parsed, RAW_TEXT)
odds = {"0": {"odds": -105, "match_type": "exact"}, "1": {"odds": -130, "match_type": "exact"}}
out = _insert_odds(INCIDENT_TEXT, parsed["picks"], odds)
checks = [
    ("no multiplied [+245]", "[+245]" not in out),
    ("spread price shown", "[-105]" in out),
    ("ML price shown", "[-130]" in out),
    ("odds idempotent", _insert_odds(out, parsed["picks"], odds) == out),
]
# Emojis: a full-game spread and ML on one side both settle at the final, so
# they reach _insert_emojis together, in index order — the same order
# _insert_odds placed the tags in, which keeps each verdict beside its own
# price. Re-inserting the same verdicts must be a no-op (daemon + tracker both
# re-edit). Mixed result = VT wins by 1-2: spread loses, ML wins.
verdicts = [(parsed["picks"][0], "LOSS", "", "NCAAF"), (parsed["picks"][1], "WIN", "", "NCAAF")]
graded = _insert_emojis(out, verdicts)
lines = graded.split("\n")
checks += [
    ("spread verdict beside the spread price", any("[-105]❌" in ln for ln in lines)),
    ("ML verdict beside the ML price", any("[-130]✅" in ln for ln in lines)),
    ("emoji re-insert is a no-op", _insert_emojis(graded, verdicts) == graded),
]
for name, ok in checks:
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}")

# Trent footer: X's pre-escaped "&amp;" must reach Telegram as one "&".
from scripts.trent_watcher import send_pick  # noqa: E402

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    asyncio.run(send_pick({"text": "-2.5 SPREAD &amp; ML 🔒 &lt;3",
                           "url": "https://x.com/BookitWithTrent/status/1"},
                          dest=0, dry_run=True))
sent = buf.getvalue()
ok = "SPREAD &amp; ML" in sent and "&amp;amp;" not in sent and "&lt;3" in sent
failures += not ok
print(f"{'PASS' if ok else 'FAIL'}  trent text escaped exactly once: {sent.strip()[:90]!r}")

print(f"\n{'ALL PASS' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
