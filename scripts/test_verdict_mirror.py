"""Fan-out verdict mirror: the first copy to resolve a leg settles its siblings.

Copies of one source pick (shared `_source_key`, identical parse) used to be graded
independently — up to 4× the ESPN context + Claude grade per leg, and a sampled
grade could diverge between copies (2026-08-24 Marlins F5 PUSH-vs-WIN split).
`find_sibling_verdict` (tracker_cache) is the lookup the grade daemon now runs
before paying for a grade; these are its guard rails.

Run:  ~/venv/bin/python scripts/test_verdict_mirror.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker_cache import find_sibling_verdict  # noqa: E402

SRC = "-1001910823870:461500"
PICK = {"description": "Falcons / Saints over 47", "bet_type": "total"}
OTHER_PICK = {"description": "Ravens -3", "bet_type": "spread"}


def entry(source_key=SRC, desc="Falcons / Saints over 47", leg=None, parsed=True):
    e = {"_source_key": source_key}
    if parsed:
        e["parsed"] = {"picks": [{"description": desc, "bet_type": "total"}]}
    if leg is not None:
        e["leg_verdicts"] = {"0": leg}
    return e


WIN = {"verdict": "WIN", "calc": "47.5 > 47 [final]", "sport": "NFL",
       "game_date": "2026-10-05", "broadcasted": True}


def main() -> int:
    failures = 0

    def check(label, ok):
        nonlocal failures
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failures += not ok

    cache = {
        "-1002486251914:4000": entry(leg=WIN),                       # resolved sibling
        "-1004427337587:480": entry(leg=None),                       # unresolved sibling
        "-1003974826106:15": entry(leg={"unknown_attempts": 2}),     # attempt-capped, no verdict
    }

    got = find_sibling_verdict(cache, SRC, "-1004427337587:480", 0, PICK)
    check("resolved sibling leg found", got is WIN)
    check("match is the raw sibling leg (caller copies the fields it wants)",
          got.get("broadcasted") is True)

    check("no source key → None",
          find_sibling_verdict(cache, None, "-1004427337587:480", 0, PICK) is None)
    check("own entry is excluded",
          find_sibling_verdict({"-1002486251914:4000": entry(leg=WIN)},
                               SRC, "-1002486251914:4000", 0, PICK) is None)
    check("different description never inherits",
          find_sibling_verdict(cache, SRC, "-1004427337587:480", 0, OTHER_PICK) is None)
    check("different source key → None",
          find_sibling_verdict(cache, "-1001910823870:999", "-1004427337587:480", 0, PICK) is None)
    check("leg index beyond sibling's picks → None",
          find_sibling_verdict(cache, SRC, "-1004427337587:480", 1, PICK) is None)

    for v, label in ((None, "unresolved"), ({"verdict": "VOID"}, "VOID"),
                     ({"verdict": "PENDING"}, "PENDING"),
                     ({"unknown_attempts": 3}, "attempt-capped")):
        c = {"-1002486251914:4000": entry(leg=v)}
        check(f"{label} sibling leg does not mirror",
              find_sibling_verdict(c, SRC, "-1004427337587:480", 0, PICK) is None)

    c = {"-1002486251914:4000": {"_source_key": SRC, "_dupe": True},
         "-1002486251914:4001": "corrupt"}
    check("parse-less / corrupt siblings are skipped",
          find_sibling_verdict(c, SRC, "-1004427337587:480", 0, PICK) is None)

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
