"""Regression: the parlay overall emoji must be idempotent across re-edits.

Incident (-1002486251914:3694 + -1004427337587:161, 2026-08-29): the grade
daemon stamped "Liu/Asakura parlay -120✅"; an overlapping tracker run (which
had independently graded the second leg) re-ran _insert_emojis over the live
text. The parlay branch skipped the header line (already emojied), the leg-line
fallback refused the same line (already emojied), and the code fell through to
lines.append(emoji) — a stray "✅" on its own line, in both fan-out copies.
Every further pass would have appended another.

Message text and picks are byte-exact copies of the live message / cache entry.
Offline — no network, no API spend.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker_format import _insert_emojis  # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


LIU_PICK = {
    "description": "Liu moneyline (parlay leg)", "sport": None,
    "bet_type": "moneyline", "is_parlay_leg": True, "period": "game",
    "teams": ["Liu"], "player": None, "prop_stat": None,
    "line": None, "direction": None,
}
ASAKURA_PICK = dict(LIU_PICK, description="Asakura moneyline (parlay leg)",
                    teams=["Asakura"])
VERDICTS = [(LIU_PICK, "WIN", "calc", "UFC", "2026-08-29"),
            (ASAKURA_PICK, "WIN", "calc", "UFC", "2026-08-29")]

CLEAN = "UFC Analyst\n\nLiu/Asakura parlay -120"
STAMPED = "UFC Analyst\n\nLiu/Asakura parlay -120✅"   # the daemon's edit

# ── first pass places exactly one emoji on the parlay line ───────────────────
out = _insert_emojis(CLEAN, VERDICTS)
check("first pass stamps the parlay line", out == STAMPED, repr(out))

# ── the incident: a second pass over stamped text must change nothing ────────
again = _insert_emojis(STAMPED, VERDICTS)
check("re-edit of stamped text is a no-op", again == STAMPED, repr(again))
check("no stray bare-emoji line", "\n✅" not in again, repr(again))

# ── LOSS verdict takes the same paths ────────────────────────────────────────
loss_v = [(p, "LOSS", c, s, d) for p, _w, c, s, d in VERDICTS]
loss_out = _insert_emojis(CLEAN, loss_v)
check("LOSS stamps once", loss_out == CLEAN + "❌", repr(loss_out))
check("LOSS re-edit is a no-op",
      _insert_emojis(loss_out, loss_v) == loss_out)

# ── fallback path (no "parlay" keyword): stamp the last leg line once ────────
NO_HEADER = "Capper\n\nLiu ML\nAsakura ML"
fb = _insert_emojis(NO_HEADER, VERDICTS)
check("fallback stamps the last leg line", fb == "Capper\n\nLiu ML\nAsakura ML✅",
      repr(fb))
check("fallback re-edit is a no-op", _insert_emojis(fb, VERDICTS) == fb,
      repr(_insert_emojis(fb, VERDICTS)))

# ── bare-append path (nothing matches): append once, then hold ───────────────
NO_MATCH_V = [({"description": "mystery leg", "is_parlay_leg": True,
                "teams": [], "player": None, "bet_type": "moneyline"},
               "WIN", "calc", "UFC", "2026-08-29")]
ba = _insert_emojis("Capper\n\nsomething unrelated", NO_MATCH_V)
check("bare append places one emoji", ba.endswith("\n✅"), repr(ba))
ba2 = _insert_emojis(ba, NO_MATCH_V)
check("bare append re-edit is a no-op", ba2 == ba, repr(ba2))

# ── mixed message: standalone emoji must not block the parlay stamp ──────────
SNELL = {"description": "Blake Snell over 8.5 Ks", "bet_type": "prop",
         "is_parlay_leg": False, "teams": ["Los Angeles Dodgers"],
         "player": "Blake Snell", "prop_stat": "K", "line": 8.5}
MIXED = "Capper\n\nSnell over 8.5 Ks\nParlay: Liu ML + Asakura ML"
mixed_v = [(SNELL, "LOSS", "calc", "MLB", "2026-08-29")] + VERDICTS
mx = _insert_emojis(MIXED, mixed_v)
check("mixed: standalone emoji placed", "Snell over 8.5 Ks❌" in mx, repr(mx))
check("mixed: parlay emoji placed", "Asakura ML✅" in mx, repr(mx))
check("mixed: re-edit is a no-op", _insert_emojis(mx, mixed_v) == mx,
      repr(_insert_emojis(mx, mixed_v)))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
