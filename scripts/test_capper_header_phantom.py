"""Regression test: the capper-name header line must never become a pick.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_capper_header_phantom.py

Forwarded posts lead with the mapping's source_prefix — the capper's name on
its own line. Zilla's "Zilla\n\nAlabama / Miss St Over 59.5 [-163]" parsed the
bare header as a second bet: "Zilla moneyline", sport=UFC, teams=["Zilla"],
no line/direction. That phantom leg matched no card, context-skipped every
tracker pass (UNKNOWN is never persisted, and a context skip records no
unknown attempt in the tracker), and sat unresolved until the nightly audit
flagged it. 15 of 16 Zilla posts parsed clean — a sampling flap, so the fix
is this deterministic backstop, not prompt text.

The net is deliberately narrow; the shapes it must NOT touch are pinned too:
a single-pick message (in an unprefixed channel a bare name line can BE the
pick — a UFC fighter's ML), a pick whose name carries real bet content, and
a message where the capper's name reappears in a bet line.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import _drop_capper_header_pick


def P(desc, teams, line=None, direction=None, player=None, bet_type="moneyline"):
    return {"description": desc, "bet_type": bet_type, "is_parlay_leg": False,
            "period": "game", "teams": teams, "player": player,
            "prop_stat": None, "line": line, "direction": direction}


INCIDENT_TEXT = ("Zilla\n\nAlabama / Miss St Over 59.5 [-163]\n\n"
                 "> 2-0 run; 6-2 off 2 wins\n> 7-2 NCAAF\n> 11-0 Saturday")

CASES = [
    # (name, capper, picks, text, expected surviving descriptions)
    (
        "the incident: bare header parsed as a UFC moneyline",
        "Zilla",
        [P("Zilla moneyline", ["Zilla"], bet_type="moneyline"),
         P("Alabama vs Mississippi State Over 59.5",
           ["Alabama Crimson Tide", "Mississippi State Bulldogs"],
           line=59.5, direction="over", bet_type="total")],
        INCIDENT_TEXT,
        ["Alabama vs Mississippi State Over 59.5"],
    ),
    (
        "teams-empty variant: description is just the header + 'moneyline'",
        "Zilla",
        [P("Zilla moneyline", []),
         P("Alabama vs Mississippi State Over 59.5",
           ["Alabama Crimson Tide", "Mississippi State Bulldogs"],
           line=59.5, direction="over", bet_type="total")],
        INCIDENT_TEXT,
        ["Alabama vs Mississippi State Over 59.5"],
    ),
    (
        "single-pick message: a bare name line can BE the pick — untouched",
        "Zilla",
        [P("Zilla moneyline", ["Zilla"])],
        "Zilla\n\n> 15-7 off 1 loss",
        ["Zilla moneyline"],
    ),
    (
        "capper name reappears in a real bet line — untouched",
        "Zilla",
        [P("Zilla moneyline", ["Zilla"]),
         P("Over 59.5", ["Alabama Crimson Tide"], line=59.5, direction="over",
           bet_type="total")],
        "Zilla\n\nZilla by KO\nAlabama Over 59.5",
        ["Zilla moneyline", "Over 59.5"],
    ),
    (
        "header-named pick carrying a real line number — untouched",
        "Zilla",
        [P("Zilla -2.5", ["Zilla"], line=-2.5, bet_type="spread"),
         P("Over 59.5", ["Alabama Crimson Tide"], line=59.5, direction="over",
           bet_type="total")],
        "Zilla\n\nAlabama Over 59.5",
        ["Zilla -2.5", "Over 59.5"],
    ),
    (
        "first line is not the capper name — untouched",
        "Empire",
        [P("Zilla moneyline", ["Zilla"]),
         P("Over 59.5", ["Alabama Crimson Tide"], line=59.5, direction="over",
           bet_type="total")],
        "Alabama -7\n\nsome text",
        ["Zilla moneyline", "Over 59.5"],
    ),
    (
        "no capper_name passed (grade_one path) — untouched",
        None,
        [P("Zilla moneyline", ["Zilla"]),
         P("Over 59.5", ["Alabama Crimson Tide"], line=59.5, direction="over",
           bet_type="total")],
        INCIDENT_TEXT,
        ["Zilla moneyline", "Over 59.5"],
    ),
    (
        "every pick an echo — never empty the message",
        "Zilla",
        [P("Zilla moneyline", ["Zilla"]), P("Zilla ML", ["Zilla"])],
        "Zilla\n\n> stats",
        ["Zilla moneyline", "Zilla ML"],
    ),
    (
        "bold header variant (**Zilla**) still recognized",
        "Zilla",
        [P("Zilla moneyline", ["Zilla"]),
         P("Over 59.5", ["Alabama Crimson Tide"], line=59.5, direction="over",
           bet_type="total")],
        "**Zilla**\n\nAlabama Over 59.5",
        ["Over 59.5"],
    ),
]


def main() -> int:
    failed = 0
    for name, capper, picks, text, expected in CASES:
        parsed = {"sport": "NCAAF", "picks": [dict(p) for p in picks]}
        _drop_capper_header_pick(parsed, text, capper)
        got = [p["description"] for p in parsed["picks"]]
        ok = got == expected
        print(("PASS" if ok else "FAIL"), "—", name)
        if not ok:
            print(f"   expected {expected}\n   got      {got}")
            failed += 1
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
