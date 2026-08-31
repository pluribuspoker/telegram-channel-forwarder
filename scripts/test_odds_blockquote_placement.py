"""Regression test: a blockquoted angle record must never take the odds tag.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_odds_blockquote_placement.py

Guards the bug where "Dbacks ML" (abbreviated team, bare moneyline) got its
[-141] stranded on the blockquote angle record below it: the parse expands
"Dbacks" to "Arizona Diamondbacks", which shares no word with the message; a
moneyline has no line number; and stripping team words from the description
leaves just "ml" — so every matching pass failed and the pick fell to
_best_content_line, whose last-content-line rule chose the angle record
(-1002486251914:3639, its fan-out sibling -1004427337587:105, and earlier
-1002486251914:3485). The emoji matcher (_match_pick_line) has excluded
blockquote lines all along, so odds and emoji disagreed about which line is
the pick.

Pins the two halves of the fix: blockquote lines are excluded from every odds
placement pass (shared _blockquote_lines helper), and _insert_odds gained the
bet-line-heuristic pass _match_pick_line already had, so both paths converge
on "Dbacks ML".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker_format import _best_content_line, _insert_emojis, _insert_odds

DBACKS_PICK = {
    "description": "Arizona Diamondbacks moneyline",
    "bet_type": "moneyline", "is_parlay_leg": False, "period": "game",
    "teams": ["Arizona Diamondbacks"], "player": None, "prop_stat": None,
    "line": None, "direction": None,
}
DBACKS_ODDS = {"0": {"odds": -141, "match_type": "exact"}}

# Byte-exact pre-tag html_text of -1002486251914:3639 (note the trailing
# space after "Dbacks ML").
DBACKS_TEXT = "TMS\n\nDbacks ML \n\n<blockquote>35-10 off 1 loss</blockquote>"

# -1002486251914:3485 — same pick, multi-line blockquote whose second line
# ("36-19 MLB Fav ML") contains both a record number and "ML", so without the
# blockquote exclusion the bet-line heuristic itself would score it highest.
DBACKS_MULTI_BQ = (
    "TMS\n\nDbacks ML \n\n<blockquote>30-9 off 1 loss (5-0 off 1 loss L30 days)\n"
    "36-19 MLB Fav ML</blockquote>"
)
DBACKS_MULTI_ODDS = {"0": {"odds": 120, "match_type": "exact"}}

# A blockquote that names the team must not attract the team-name pass either.
SD_PICK = {
    "description": "San Diego Padres moneyline",
    "bet_type": "moneyline", "is_parlay_leg": False, "period": "game",
    "teams": ["San Diego Padres"], "player": None, "prop_stat": None,
    "line": None, "direction": None,
}
SD_TEXT = "OG\n\nSD ML\n\n<blockquote>Padres 30-9 at home</blockquote>"
SD_ODDS = {"0": {"odds": -115, "match_type": "exact"}}

# Pure-slang pick with no bet-line keywords at all: the single-pick fallback
# must land on the last NON-blockquote content line.
SLANG_PICK = {
    "description": "American League parlay of moneylines",
    "bet_type": "moneyline", "is_parlay_leg": False, "period": "game",
    "teams": [], "player": None, "prop_stat": None,
    "line": None, "direction": None,
}
SLANG_TEXT = "Cap\n\nnuking the AL tonight\n\n<blockquote>5-0 on nukes</blockquote>"
SLANG_ODDS = {"0": {"odds": 250, "match_type": "exact"}}

FAILURES = []


def check(name, got, want):
    if got == want:
        print(f"  ok  {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL  {name}\n      got:  {got!r}\n      want: {want!r}")


def main():
    # 1. The reported message: tag on the pick line, blockquote untouched.
    got = _insert_odds(DBACKS_TEXT, [DBACKS_PICK], DBACKS_ODDS)
    want = "TMS\n\nDbacks ML [-141]\n\n<blockquote>35-10 off 1 loss</blockquote>"
    check("dbacks tag on pick line", got, want)

    # 2. Idempotent — a second pass changes nothing (the tracker re-runs
    # _insert_odds on the live text every cycle).
    check("dbacks idempotent", _insert_odds(got, [DBACKS_PICK], DBACKS_ODDS), want)

    # 3. The verdict emoji lands on the same line as the tag.
    graded = _insert_emojis(got, [(DBACKS_PICK, "WIN", "", "MLB")])
    check(
        "emoji converges on the tagged line",
        graded,
        "TMS\n\nDbacks ML [-141]✅\n\n<blockquote>35-10 off 1 loss</blockquote>",
    )

    # 4. Multi-line blockquote: the "36-19 MLB Fav ML" record line must lose to
    # the real pick line even though it out-scores it on bet-line keywords.
    got = _insert_odds(DBACKS_MULTI_BQ, [DBACKS_PICK], DBACKS_MULTI_ODDS)
    want = (
        "TMS\n\nDbacks ML [+120]\n\n<blockquote>30-9 off 1 loss (5-0 off 1 loss L30 days)\n"
        "36-19 MLB Fav ML</blockquote>"
    )
    check("multi-line blockquote never takes the tag", got, want)

    # 5. Team name appearing only inside the blockquote must not attract the tag.
    got = _insert_odds(SD_TEXT, [SD_PICK], SD_ODDS)
    want = "OG\n\nSD ML [-115]\n\n<blockquote>Padres 30-9 at home</blockquote>"
    check("team word in blockquote ignored", got, want)

    # 6. Single-pick fallback skips blockquote lines.
    got = _insert_odds(SLANG_TEXT, [SLANG_PICK], SLANG_ODDS)
    want = "Cap\n\nnuking the AL tonight [+250]\n\n<blockquote>5-0 on nukes</blockquote>"
    check("best-content fallback skips blockquote", got, want)
    check(
        "_best_content_line skips blockquote",
        _best_content_line(SLANG_TEXT.split("\n")),
        2,
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("all placement checks passed")


if __name__ == "__main__":
    main()
