"""Regression test: the period qualifier survives into the rendered pick label.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_period_tag.py

`_format_pick` computes `period_tag` once at the top and every branch is
supposed to interpolate it — but the total/team_total and player-prop branches
built their string without it, so ".5u Wings 1H u79.5" broadcast as
"Dallas Wings U79.5". A first-half under and a full-game under are different
bets with different lines; dropping the qualifier reports the wrong one, and
the result reads as plausible so nothing downstream flags it.

The omission is invisible in isolation, which is why this pins EVERY bet type
rather than the one that broke: spread and moneyline were already correct, and
their output is the format the others are matched against ("Team 1H -3.5").
`_format_pick` also feeds sheets.py, so the Google Sheets log inherits it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit import _format_pick


def P(**kw):
    base = {
        "description": "", "bet_type": "", "is_parlay_leg": False, "period": "game",
        "teams": [], "player": None, "prop_stat": None, "line": None, "direction": None,
    }
    base.update(kw)
    return base


CASES = [
    # ── the reported bug: WNBA first-half team total ──────────────────────────
    ("total 1h (reported)",
     P(bet_type="total", teams=["Dallas Wings"], line=79.5, direction="under",
       period="1h", sport="WNBA", description="Dallas Wings 1H under 79.5 -110"),
     "Dallas Wings 1H U79.5"),
    ("team_total 1h",
     P(bet_type="team_total", teams=["Dallas Wings"], line=79.5, direction="under",
       period="1h", sport="WNBA"),
     "Dallas Wings 1H U79.5"),
    ("total 1q, two teams",
     P(bet_type="total", teams=["Calgary Stampeders", "Edmonton Elks"], line=10.5,
       direction="over", period="1q", sport="CFL"),
     "Calgary Stampeders/Edmonton Elks 1Q O10.5"),
    # Headless game total: period_tag is the entire prefix.
    ("total 1h, no team",
     P(bet_type="total", line=8.5, direction="over", period="1h", sport="NBA"),
     "1H O8.5"),
    # MLB/KBO 1H renders as F5 — the existing special case must still apply here.
    ("total 1h MLB -> F5",
     P(bet_type="total", teams=["Toronto Blue Jays"], line=4.5, direction="over",
       period="1h", sport="MLB"),
     "Toronto Blue Jays F5 O4.5"),
    ("team_total 1h KBO -> F5",
     P(bet_type="team_total", teams=["LG Twins"], line=2.5, direction="under",
       period="1h", sport="KBO"),
     "LG Twins F5 U2.5"),
    ("total with prop_stat keeps stat after line",
     P(bet_type="total", teams=["A", "B"], line=1.5, direction="over",
       period="1h", sport="Soccer", prop_stat="corners"),
     "A/B 1H O1.5 corners"),

    # ── sibling branch: player props ──────────────────────────────────────────
    ("player prop 1h",
     P(bet_type="prop", player="Paige Bueckers", line=10.5, direction="over",
       prop_stat="points", period="1h", sport="WNBA"),
     "Paige Bueckers 1H O10.5 points"),
    ("player prop 1h MLB -> F5",
     P(bet_type="prop", player="Shohei Ohtani", line=1.5, direction="over",
       prop_stat="strikeouts", period="1h", sport="MLB"),
     "Shohei Ohtani F5 O1.5 strikeouts"),
    ("player prop, no line/direction",
     P(bet_type="prop", player="Josh Allen", period="1h", sport="NFL"),
     "Josh Allen 1H"),

    # ── branches that were already correct — pinned so they stay the analog ───
    ("spread 1h", P(bet_type="spread", teams=["Edmonton Elks"], line=2.5,
                    period="1h", sport="CFL"), "Edmonton Elks 1H +2.5"),
    ("spread 1q negative", P(bet_type="spread", teams=["Montreal Alouettes"],
                             line=-2.5, period="1q", sport="CFL"),
     "Montreal Alouettes 1Q -2.5"),
    ("moneyline 1h", P(bet_type="moneyline", teams=["Toronto Argonauts"],
                       period="1h", sport="CFL",
                       description="Toronto Argonauts 1st Half Moneyline -115"),
     "Toronto Argonauts 1H ML"),
    ("moneyline 1q 3-way", P(bet_type="moneyline", teams=["Winnipeg Blue Bombers"],
                             period="1q", sport="CFL",
                             description="Winnipeg Blue Bombers 1st Quarter Moneyline 3-way (-145)"),
     "Winnipeg Blue Bombers 1Q 3-way ML"),
    ("double_chance 1h", P(bet_type="double_chance", teams=["Arsenal"],
                           period="1h", sport="Soccer"), "Arsenal 1H DC"),
    ("draw_no_bet 1h", P(bet_type="draw_no_bet", teams=["Arsenal"],
                         period="1h", sport="Soccer"), "Arsenal 1H DNB"),
    ("team prop 1h", P(bet_type="prop", teams=["Arsenal", "Chelsea"],
                       prop_stat="BTTS", direction="over", period="1h",
                       sport="Soccer"), "Arsenal vs Chelsea 1H BTTS Yes"),

    # ── period="game" must render exactly as before (no stray spacing) ────────
    ("total game", P(bet_type="total", teams=["Dallas Wings"], line=79.5,
                     direction="under", period="game", sport="WNBA"),
     "Dallas Wings U79.5"),
    ("total game, two teams", P(bet_type="total", teams=["A", "B"], line=8.5,
                                direction="over", period="game", sport="NBA"),
     "A/B O8.5"),
    ("total game, no team", P(bet_type="total", line=8.5, direction="over",
                              period="game", sport="NBA"), "O8.5"),
    ("player prop game", P(bet_type="prop", player="Luka Doncic", line=27.5,
                           direction="over", prop_stat="points", period="game",
                           sport="NBA"), "Luka Doncic O27.5 points"),
    ("player prop game, no line", P(bet_type="prop", player="Josh Allen",
                                    period="game", sport="NFL"), "Josh Allen"),
    ("spread game", P(bet_type="spread", teams=["Dallas Wings"], line=-3.5,
                      period="game", sport="WNBA"), "Dallas Wings -3.5"),
    ("moneyline game", P(bet_type="moneyline", teams=["Dallas Wings"],
                         period="game", sport="WNBA",
                         description="Dallas Wings moneyline -108"), "Dallas Wings ML"),
    # period missing entirely (older cache entries predate the field)
    ("total, period absent",
     {"bet_type": "total", "teams": ["Dallas Wings"], "line": 79.5,
      "direction": "under", "sport": "WNBA"}, "Dallas Wings U79.5"),
]


def main() -> int:
    failures = 0
    for label, pick, expected in CASES:
        got = _format_pick(pick)
        ok = got == expected
        if not ok:
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            print(f"   expected: {expected!r}")
            print(f"   got:      {got!r}")

    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
