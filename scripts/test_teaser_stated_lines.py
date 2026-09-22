"""Regression test: teaser legs grade at their STATED (already-teased) lines.

Self-contained (no network, no Claude) — run directly:

    ~/venv/bin/python scripts/test_teaser_stated_lines.py

Incident 2026-09-20 (FCS, -1002486251914:3867): "7 pt teaser: Eagles PK /
Rams +0.5" parsed as Eagles +7 / Rams +7.5 — the model read the stated lines
as pre-tease and added the 7 points. A teaser states the TEASED line, so every
teaser leg graded ~7 points too generously: a leg losing by 1-7 would grade
WIN. Verdicts held there only because both teams won outright.

Pinned here: _fix_teaser_stated_lines re-pins parlay-leg spreads to the one
number the message states (PK/pick'em = 0), fails open on anything ambiguous,
and the corrected line grades correctly through the spread math.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import _fix_teaser_stated_lines
from scores import try_early_grade_math

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  -> {detail}"))
    if not cond:
        failures.append(name)


# The real message text at parse time (live html_text minus the later emoji
# edits and blockquote tags — Telethon raw_text carries the lines bare).
FCS_TEXT = (
    "FCS\n\n5U (whale) 7 pt teaser: \nEagles PK / Rams +0.5\n\n1U Bucs -8\n\n"
    "1-0 on 5U picks tracked\n29-12 this month\n7-1 NFL"
)


def fcs_parse() -> dict:
    """The real (wrong) parse, verbatim from parse_cache -1002486251914:3867."""
    return {"sport": "NFL", "picks": [
        {"description": "Eagles +7 (7pt teaser leg)", "sport": "NFL",
         "bet_type": "spread", "is_parlay_leg": True, "period": "game",
         "teams": ["Philadelphia Eagles"], "player": None, "prop_stat": None,
         "line": 7, "direction": None},
        {"description": "Rams +7.5 (7pt teaser leg)", "sport": "NFL",
         "bet_type": "spread", "is_parlay_leg": True, "period": "game",
         "teams": ["Rams"], "player": None, "prop_stat": None,
         "line": 7.5, "direction": None},
        {"description": "Bucs -8", "sport": "NFL",
         "bet_type": "spread", "is_parlay_leg": False, "period": "game",
         "teams": ["Bucs"], "player": None, "prop_stat": None,
         "line": -8, "direction": None},
    ]}


# ── 1: the incident corrects to the stated lines ─────────────────────────────

parsed = fcs_parse()
_fix_teaser_stated_lines(parsed, FCS_TEXT)
eagles, rams, bucs = parsed["picks"]
check("Eagles PK re-pins +7 -> 0", eagles["line"] == 0.0, repr(eagles["line"]))
check("Eagles description says PK",
      eagles["description"] == "Philadelphia Eagles PK (teaser leg)",
      eagles["description"])
check("Rams re-pins +7.5 -> +0.5", rams["line"] == 0.5, repr(rams["line"]))
check("Rams description says +0.5",
      rams["description"] == "Rams +0.5 (teaser leg)", rams["description"])
check("standalone Bucs untouched (not a teaser leg)",
      bucs["line"] == -8 and bucs["description"] == "Bucs -8", repr(bucs))

# ── 2: idempotent + already-correct is a no-op ───────────────────────────────

snapshot = [dict(p) for p in parsed["picks"]]
_fix_teaser_stated_lines(parsed, FCS_TEXT)
check("second pass changes nothing", parsed["picks"] == snapshot)

# ── 3: guards fail open ──────────────────────────────────────────────────────

p = fcs_parse()
_fix_teaser_stated_lines(p, FCS_TEXT.replace("teaser", "special"))
check("no 'teaser' in the message -> untouched", p["picks"][0]["line"] == 7)

p = fcs_parse()
_fix_teaser_stated_lines(p, "6 team teaser\nEagles -3 +7 tonight\nRams +0.5")
check("segment naming two lines -> that pick untouched, sibling still fixed",
      p["picks"][0]["line"] == 7 and p["picks"][1]["line"] == 0.5,
      f"{p['picks'][0]['line']} / {p['picks'][1]['line']}")

p = fcs_parse()
_fix_teaser_stated_lines(p, "teaser\nEagles PK -2.5")
check("PK plus a number in one segment -> ambiguous, untouched",
      p["picks"][0]["line"] == 7)

p = fcs_parse()
_fix_teaser_stated_lines(p, "teaser play\nRams 29-12 on the season\nRams +0.5")
check("record fragment '29-12' never reads as -12",
      p["picks"][1]["line"] == 0.5, repr(p["picks"][1]["line"]))

p = fcs_parse()
_fix_teaser_stated_lines(p, "teaser\nEagles -3 (-110)")
check("a price (abs >= 60) is not a candidate; -3 still corrects",
      p["picks"][0]["line"] == -3.0, repr(p["picks"][0]["line"]))

p = fcs_parse()
p["picks"][0]["bet_type"] = "moneyline"
_fix_teaser_stated_lines(p, FCS_TEXT)
check("non-spread parlay leg untouched", p["picks"][0]["line"] == 7)

# ── 4: the corrected line grades correctly (the class this kills) ────────────

def sb(away_name, away_score, home_name, home_score):
    def comp(name, score, side):
        return {"homeAway": side, "team": {"displayName": name},
                "score": str(score),
                "linescores": [{"value": float(score), "displayValue": str(score)}]}
    return {"events": [{
        "id": "401001",
        "status": {"type": {"state": "post", "completed": True}},
        "competitions": [{"competitors": [
            comp(away_name, away_score, "away"),
            comp(home_name, home_score, "home"),
        ]}],
    }]}


parsed = fcs_parse()
_fix_teaser_stated_lines(parsed, FCS_TEXT)
eagles = parsed["picks"][0]

win = try_early_grade_math("NFL", eagles, sb("Philadelphia Eagles", 24, "Los Angeles Rams", 20))
check("Eagles PK, won 24-20 -> WIN", bool(win) and win[0] == "WIN", repr(win))

loss = try_early_grade_math("NFL", eagles, sb("Philadelphia Eagles", 20, "Los Angeles Rams", 23))
check("Eagles PK, lost 20-23 -> LOSS (old +7 parse graded this WIN)",
      bool(loss) and loss[0] == "LOSS", repr(loss))

print()
if failures:
    print(f"❌ {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ all teaser stated-line cases pass")
