"""Regression: a multi-pick message with two bets on the SAME game must get
each verdict emoji on its own bet's line, regardless of grading order.

Incident (-1002486251914:3774 / -1004427337587:243, 2026-09-06): Empire posted
"Notre Dame -20.5" and "Notre Dame / Wisconsin u48" in one message. The u48
died mid-game (13+40=53) and graded ~9 min before the final; _match_pick_line
pass 1 matched it by team terms to the FIRST "Notre Dame" line — the spread —
so the ❌ landed there. When the spread won at the final, its line was
"unavailable" (already carried an emoji) and the ✅ fell through to the u48
line: a clean swap, ❌ on the winner and ✅ on the loser, in both channels.
Same-cycle grading masks the bug because picks parse in message order, so the
tie-break in _prefer_bet_line must make placement order-independent.

Message text and parsed picks are byte-exact copies of the live incident data.
Offline — no network, no API spend.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker_format import _insert_emojis, _match_pick_line  # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


SPREAD_PICK = {
    "description": "Notre Dame -20.5 (-120) for 0.5u", "sport": "NCAAF",
    "bet_type": "spread", "is_parlay_leg": False, "period": "game",
    "teams": ["Notre Dame Fighting Irish"], "player": None,
    "prop_stat": None, "line": -20.5, "direction": None,
}
TOTAL_PICK = {
    "description": "Notre Dame vs Wisconsin Under 48 (-130) for 0.5u", "sport": "NCAAF",
    "bet_type": "total", "is_parlay_leg": False, "period": "game",
    "teams": ["Notre Dame Fighting Irish", "Wisconsin Badgers"], "player": None,
    "prop_stat": None, "line": 48, "direction": "under",
}

EMPIRE_HTML = "Empire\n\nNotre Dame -20.5 -120 .5u\nNotre Dame / Wisconsin u48 -130 .5u"
FIXED_HTML = "Empire\n\nNotre Dame -20.5 -120 .5u✅\nNotre Dame / Wisconsin u48 -130 .5u❌"

# ── the incident: split-wave grading (total mid-game, spread at final) ───────
wave1 = _insert_emojis(EMPIRE_HTML, [
    (SPREAD_PICK, "PENDING", "", "NCAAF", "2026-09-06"),
    (TOTAL_PICK, "LOSS", "[mid-game] 13+40=53 vs 48", "NCAAF", "2026-09-06"),
])
check("wave 1: ❌ on the u48 line", "u48 -130 .5u❌" in wave1, repr(wave1))
check("wave 1: spread line untouched", "-120 .5u\n" in wave1, repr(wave1))

wave2 = _insert_emojis(wave1, [
    (SPREAD_PICK, "WIN", "[final] 41-20.5 vs 13 -> +7.5", "NCAAF", "2026-09-06"),
])
check("wave 2: ✅ on the spread line, full text exact", wave2 == FIXED_HTML, repr(wave2))

# ── same-cycle grading must produce the identical text, in either order ──────
both = _insert_emojis(EMPIRE_HTML, [
    (SPREAD_PICK, "WIN", "", "NCAAF", "2026-09-06"),
    (TOTAL_PICK, "LOSS", "", "NCAAF", "2026-09-06"),
])
check("one wave, message order", both == FIXED_HTML, repr(both))

both_rev = _insert_emojis(EMPIRE_HTML, [
    (TOTAL_PICK, "LOSS", "", "NCAAF", "2026-09-06"),
    (SPREAD_PICK, "WIN", "", "NCAAF", "2026-09-06"),
])
check("one wave, reversed order", both_rev == FIXED_HTML, repr(both_rev))

# ── idempotency: re-editing the finished message changes nothing ─────────────
again = _insert_emojis(FIXED_HTML, [
    (SPREAD_PICK, "WIN", "", "NCAAF", "2026-09-06"),
    (TOTAL_PICK, "LOSS", "", "NCAAF", "2026-09-06"),
])
check("idempotent on finished text", again == FIXED_HTML, repr(again))

# ── direction tie-break: same number, opposite sides ─────────────────────────
OVER_PICK = dict(TOTAL_PICK, description="Notre Dame 1H Over 24.5",
                 line=24.5, direction="over")
UNDER_PICK = dict(TOTAL_PICK, description="Notre Dame 1H Under 24.5",
                  line=24.5, direction="under")
TT_HTML = "Cap\n\nNotre Dame 1H o24.5\nNotre Dame 1H u24.5"
lines = TT_HTML.split("\n")
check("direction: over picks the o-line", _match_pick_line(lines, OVER_PICK) == 2)
check("direction: under picks the u-line", _match_pick_line(lines, UNDER_PICK) == 3)

# ── number guards: -20 must not claim the -20.5 line ─────────────────────────
TONY_PICK = dict(SPREAD_PICK, description="Notre Dame -20", line=-20)
G_HTML = "Cap\n\nNotre Dame -20.5 -120\nNotre Dame -20 -141"
g_lines = G_HTML.split("\n")
check("exact number: -20 picks its own line", _match_pick_line(g_lines, TONY_PICK) == 3)
check("exact number: -20.5 picks its own line",
      _match_pick_line(g_lines, SPREAD_PICK) == 2)

# ── re-insert must be a no-op, never a cascade (-1002486251914:3755) ─────────
# The tracker edited HC's ❌ in at 21:00; at 21:48 the daemon re-inserted HC
# (graded but not yet broadcast, so still in emoji_verdicts) against text that
# already carried it. HC's line was "unavailable", the cascade stamped a second
# ❌ on SDSU's line via the bet-line heuristic, and SDSU's real ✅ then found
# no line and was dropped. Byte-exact live text, verdicts from the cache.
AC_PICKS = [
    {"description": "Alabama 1H -14.5 (-145)", "teams": ["Alabama Crimson Tide"],
     "line": -14.5},
    {"description": "Oregon State 1H +11.5 (-110)", "teams": ["Oregon State Beavers"],
     "line": 11.5},
    {"description": "Boston College +7.5 (-110)", "teams": ["Boston College Eagles"],
     "line": 7.5},
    {"description": "Houston Christian 1H +11.5 (-110)",
     "teams": ["Houston Christian Huskies"], "line": 11.5},
    {"description": "South Dakota State 1H +7.5 (-110)",
     "teams": ["South Dakota State Jackrabbits"], "line": 7.5},
]
for p in AC_PICKS:
    p.update({"sport": "NCAAF", "bet_type": "spread", "is_parlay_leg": False,
              "period": "1h", "player": None, "prop_stat": None, "direction": None})
AC_PRE = ("Andrew Cunningham\n\n• Alabama 1H -14.5 (-145) / (1.45u)✅\n\n"
          "• Oregon State 1H +11.5 (-110) / (1.1u)❌\n\n"
          "• Boston College +7.5 (-110) / (1.1u)❌\n\n"
          "• Houston Christian 1H +11.5 (-110) / (1.1u)❌\n\n"
          "• South Dakota St 1H +7.5 (-110) / (1.1u)\n\n"
          "<blockquote>NCAAF stats</blockquote>")
ac_out = _insert_emojis(AC_PRE, [
    (AC_PICKS[3], "LOSS", "", "NCAAF", "2026-09-05"),
    (AC_PICKS[4], "WIN", "", "NCAAF", "2026-09-05"),
])
check("re-insert: exactly three ❌, none doubled", ac_out.count("❌") == 3
      and "❌❌" not in ac_out, repr(ac_out))
check("re-insert: SDSU gets its ✅", "St 1H +7.5 (-110) / (1.1u)✅" in ac_out,
      repr(ac_out))

# ── identity matches claim lines before the pass-5 heuristic ─────────────────
# "REDSOX" (one word) identity-matches nothing, so a greedy loop pass-5'd its
# ❌ onto the Mariners line (higher bet-line score) and the Mariners pick then
# cascaded to the Red Sox line — an invisible cross-swap while both verdicts
# were LOSS, a visible one the day they differ. Identity-first ordering binds
# Mariners to its own line, Red Sox to the leftover (-1002486251914:3710).
REDSOX_PICK = {
    "description": "Boston Red Sox moneyline", "sport": "MLB",
    "bet_type": "moneyline", "is_parlay_leg": False, "period": "game",
    "teams": ["Boston Red Sox"], "player": None, "prop_stat": None,
    "line": None, "direction": None,
}
MARINERS_PICK = dict(REDSOX_PICK, description="Seattle Mariners F5 moneyline",
                     teams=["Seattle Mariners"], period="1h")
CR_HTML = "CashRace\n\nMAX : REDSOX ML\nMAX : MARINERS F5 ML [-141]"
CR_DONE = "CashRace\n\nMAX : REDSOX ML❌\nMAX : MARINERS F5 ML [-141]❌"
cr_out = _insert_emojis(CR_HTML, [
    (REDSOX_PICK, "LOSS", "", "MLB", "2026-08-30"),
    (MARINERS_PICK, "LOSS", "", "MLB", "2026-08-30"),
])
check("identity-first: both lines get ❌", cr_out == CR_DONE, repr(cr_out))
cr_again = _insert_emojis(CR_DONE, [
    (REDSOX_PICK, "LOSS", "", "MLB", "2026-08-30"),
    (MARINERS_PICK, "LOSS", "", "MLB", "2026-08-30"),
])
check("identity-first: re-insert is a no-op", cr_again == CR_DONE, repr(cr_again))

# ── single-bet message: unchanged first-match behavior ───────────────────────
YDC_PICK = {
    "description": "Wisconsin / Notre Dame Under 48 (3u supermax)", "sport": "NCAAF",
    "bet_type": "total", "is_parlay_leg": False, "period": "game",
    "teams": ["Wisconsin Badgers", "Notre Dame Fighting Irish"], "player": None,
    "prop_stat": None, "line": 48, "direction": "under",
}
YDC_HTML = ("YDC\n\nWisconsin / Notre Dame under 48 3u supermax [-143]\n\n"
            "<blockquote>NCAAF unders: 9-2 (all were 1u plays)\n"
            "3u supermax: 13-1 off 1 win // 3-0 totals</blockquote>")
ydc_out = _insert_emojis(YDC_HTML, [(YDC_PICK, "LOSS", "", "NCAAF", "2026-09-06")])
check("single pick: ❌ after the odds tag", "[-143]❌" in ydc_out, repr(ydc_out))
check("single pick: blockquote untouched", "totals</blockquote>" in ydc_out
      and "totals❌" not in ydc_out, repr(ydc_out))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
