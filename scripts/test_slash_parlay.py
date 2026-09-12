"""Regression test: slash-separated selections on one bet line are parlay legs.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_slash_parlay.py

UFC Analyst posted "Gantt / Chairez ML" with his stats card attached. The card
made has_media true, so claude_parse applied the bet-slip preamble, whose
"only set is_parlay_leg=true when the slip is ONE combined ticket" was answered
by an image holding no ticket at all — both legs came back is_parlay_leg=false.

Two things then went wrong, and only the first is visible:
  * _insert_odds took the standalone path, stamped the FIRST leg's price on the
    shared line and dropped the second, so a -122 parlay displayed as [-430]
    (Gantt -430 x Chairez -210);
  * each leg would have graded as its own wager, so a 1-1 split posts one win
    and one loss instead of a single parlay loss.

His two previous parlays survived the same preamble only because the word
"parlay" was literally in the text ("Ko/Duncan parlay -101"); this one wasn't.

The net is deliberately narrow, so the shapes it must NOT touch are pinned here
too: a slash separating a market from its unit sizing puts every pick in
segment 0, and a game-title slash parses to a single pick.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import _mark_slash_parlay_legs
from common import parlay_combined_odds
from tracker_format import _insert_odds


def P(desc, teams, player=None, parlay=False):
    return {"description": desc, "bet_type": "moneyline", "is_parlay_leg": parlay,
            "period": "game", "teams": teams, "player": player, "prop_stat": None,
            "line": None, "direction": None}


def flags(parsed, text):
    _mark_slash_parlay_legs(parsed, text)
    return [p["is_parlay_leg"] for p in parsed["picks"]]


CASES = [
    # (name, picks, text, expected flags)
    (
        "the incident: two fighters, one slash line, no 'parlay' word",
        [P("Gantt moneyline", ["Gantt"]), P("Chairez moneyline", ["Chairez"])],
        "UFC Analyst\n\nGantt / Chairez ML\n\n> 15-7 off 1 loss\n> 35-13 parlays",
        [True, True],
    ),
    (
        "already flagged by the model — left alone",
        [P("Ko moneyline", ["Ko"], parlay=True), P("Duncan moneyline", ["Duncan"], parlay=True)],
        "UFC Analyst\n\nKo/Duncan parlay -101",
        [True, True],
    ),
    (
        "slash separates the market from unit sizing — both picks in segment 0",
        [P("Hamilton Tiger-Cats moneyline", ["Hamilton Tiger-Cats"]),
         P("Hamilton Tiger-Cats 1Q spread +0.5", ["Hamilton Tiger-Cats"])],
        "Andrew Cunningham\n\n• Tiger-Cats ML (+200) / (3.5u to win 7)\n"
        "• Tiger-Cats 1Q +0.5 (-120)",
        [False, False],
    ),
    (
        "same team on both sides of the slash — ambiguous, never promoted",
        [P("Florida State +4.5", ["Florida State"]), P("Florida State ML", ["Florida State"])],
        "Andrew Cunningham\n\n• Florida St +4.5 (-130) / (2.6u)",
        [False, False],
    ),
    (
        "game-title slash with one bet — single pick, never reaches the test",
        [P("Pistons/Magic over 208.5", ["Detroit Pistons", "Orlando Magic"])],
        "Pistons / Magic o208.5",
        [False],
    ),
    (
        "no slash at all",
        [P("Boston Red Sox moneyline", ["Boston Red Sox"]),
         P("Seattle Mariners F5 moneyline", ["Seattle Mariners"])],
        "CashRace\n\nMAX : MARINERS F5 ML\nRed Sox ML",
        [False, False],
    ),
    (
        "the blockquote's 'parlays' record line is never the bet line",
        [P("Gantt moneyline", ["Gantt"]), P("Chairez moneyline", ["Chairez"])],
        "UFC Analyst\n\nGantt ML\nChairez ML\n> 35-13 parlays / 15-7 off 1 loss",
        [False, False],
    ),
]

fails = 0
for name, picks, text, expected in CASES:
    got = flags({"picks": picks}, text)
    ok = got == expected
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      expected {expected}  got {got}")

# End-to-end: the promoted parse must make _insert_odds render the parlay price
# on the bet line — the tag the operator actually sees.
picks = [P("Gantt moneyline", ["Gantt"]), P("Chairez moneyline", ["Chairez"])]
parsed = {"picks": picks}
_mark_slash_parlay_legs(parsed, "UFC Analyst\n\nGantt / Chairez ML\n\n> 35-13 parlays")
html = ("UFC Analyst\n\nGantt / Chairez ML\n\n"
        "<blockquote>15-7 off 1 loss\n35-13 parlays</blockquote>")
odds = {"0": {"odds": -430, "match_type": "exact"}, "1": {"odds": -210, "match_type": "exact"}}
out = _insert_odds(html, picks, odds)
bet_line = out.split("\n")[2]
combined = parlay_combined_odds([-430, -210])
ok = bet_line == f"Gantt / Chairez ML [{combined}]"
fails += not ok
print(f"{'PASS' if ok else 'FAIL'}  rendered tag is the combined parlay price\n"
      f"      expected {f'Gantt / Chairez ML [{combined}]'!r}  got {bet_line!r}")

# ...and stays there: the tracker re-derives the edit from live text every cycle.
ok = _insert_odds(out, picks, odds) == out
fails += not ok
print(f"{'PASS' if ok else 'FAIL'}  placement is idempotent across a second pass")

# The angle record must never attract the tag.
ok = "35-13 parlays [" not in out
fails += not ok
print(f"{'PASS' if ok else 'FAIL'}  blockquote angle record left untagged")

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
