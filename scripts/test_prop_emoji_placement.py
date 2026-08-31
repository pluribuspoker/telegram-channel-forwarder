"""Regression: verdict emojis / odds tags must never land on link lines,
and short prop stats must match as words, not substrings.

Incident (-1004394797084:70, 2026-08-25): "Life on line. Snell getting 9.
[-105]" got its ❌ appended to the '🔗 View on X' attribution line. The
emoji matcher's pass 1 demanded prop_stat "k" on the player line (Trent
never states the stat), then pass 1b substring-matched the k inside
"BookitWithTrent" in the URL. The sibling message graded right only by
luck — "Skubal" contains a k. _insert_odds had no link-line guard either
(a status id contains most integer bet lines as substrings).

Message texts are byte-exact copies of the live messages.
Offline — no network, no API spend.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker_format import (  # noqa: E402
    _best_content_line,
    _insert_emojis,
    _insert_odds,
    _is_link_line,
    _match_pick_line,
    _prop_stat_in_line,
)

passed = failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


SNELL_PICK = {
    "description": "Blake Snell over 8.5 Pitcher Ks", "sport": None,
    "bet_type": "prop", "is_parlay_leg": False, "period": "game",
    "teams": ["Los Angeles Dodgers"], "player": "Blake Snell",
    "prop_stat": "K", "line": 8.5, "direction": "over",
}
SKUBAL_PICK = dict(SNELL_PICK, description="Tarik Skubal over 8.5 Pitcher Ks",
                   player="Tarik Skubal")

SNELL_HTML = ('◼️ Trent\n\nLife on line. Snell getting 9. [-105]\n\n'
              '<a href="https://x.com/BookitWithTrent/status/2091582265528557787">'
              '🔗 View on X</a>')
SKUBAL_HTML = ('◼️ Trent\n\nLife on line. Skubal getting 9.\n\n'
               '<a href="https://x.com/BookitWithTrent/status/2091273929348227453">'
               '🔗 View on X</a>')

# ── the incident: emoji must land on the pick line, not the link ─────────────
out = _insert_emojis(SNELL_HTML, [(SNELL_PICK, "LOSS", "calc", "MLB", "2026-08-23")])
check("Snell ❌ on the pick line", "[-105]❌" in out, repr(out))
check("Snell link line untouched", "</a>❌" not in out and "X❌" not in out, repr(out))

out69 = _insert_emojis(SKUBAL_HTML, [(SKUBAL_PICK, "WIN", "calc", "MLB", "2026-08-22")])
check("Skubal ✅ on the pick line", "getting 9.✅" in out69, repr(out69))

# Idempotent: re-running over the corrected text must not move or duplicate.
check("emoji insert idempotent",
      _insert_emojis(out, [(SNELL_PICK, "LOSS", "calc", "MLB", "2026-08-23")]) == out)

# ── matcher internals ────────────────────────────────────────────────────────
check("link line detected (anchor)",
      _is_link_line('<a href="https://x.com/x/status/1">🔗 View on X</a>'))
check("link line detected (bare url)", _is_link_line("  https://x.com/foo"))
check("pick line not a link line", not _is_link_line("Life on line. Snell getting 9."))

check("short stat is word-matched: no k in BookitWithTrent",
      not _prop_stat_in_line("k", '<a href="https://x.com/bookitwithtrent/status/2">🔗 view on x</a>'))
check("short stat is word-matched: no k in skubal",
      not _prop_stat_in_line("k", "life on line. skubal getting 9."))
check("short stat matches standalone token", _prop_stat_in_line("k", "snell over 8.5 k"))
check("short stat matches pluralised token", _prop_stat_in_line("k", "snell over 8.5 ks"))
check("long stat still substring-matched",
      _prop_stat_in_line("both to score", "both to score and win"))

lines = SNELL_HTML.split("\n")
check("player-name line matched without stated stat",
      _match_pick_line(lines, SNELL_PICK) == 2, str(_match_pick_line(lines, SNELL_PICK)))
check("_best_content_line skips the link", _best_content_line(lines) == 2)

# Team-level prop keeps the stat gate: the team-name header must NOT match
# when the stat sits on its own line (BTTS-style layouts).
btts_lines = ["Cap", "", "FRANCE x SPAIN", "", "CLICK BTTS AND MOVE."]
btts_pick = {"description": "France vs Spain BTTS", "bet_type": "prop",
             "teams": ["France", "Spain"], "player": None, "prop_stat": "BTTS"}
check("team prop still gated to the stat line",
      _match_pick_line(btts_lines, btts_pick) == 4,
      str(_match_pick_line(btts_lines, btts_pick)))

# ── odds placement immune to link lines ──────────────────────────────────────
# Integer bet line "265" is a substring of the status id — pass 4 must not
# tag the URL.
tricky = ('Cap\n\nTwolves / Sixers under 265\n\n'
          '<a href="https://x.com/cap/status/2091582265528557787">🔗 View on X</a>')
tricky_pick = {"description": "Minnesota Timberwolves / Philadelphia 76ers under 265",
               "bet_type": "total", "teams": ["Minnesota Timberwolves",
                                              "Philadelphia 76ers"],
               "player": None, "line": 265}
odds_out = _insert_odds(tricky, [tricky_pick], {"0": {"odds": -110, "match_type": "exact"}})
check("odds tag on the bet line, not the URL",
      "under 265 [-110]" in odds_out and "</a> [-110]" not in odds_out, repr(odds_out))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
