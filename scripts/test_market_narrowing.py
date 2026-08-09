"""Offline guard: the narrowed odds request must contain every market the
matching `_lookup_*` actually reads.

Requests used to ask for every period variant of a bet type (8-34 markets) and
the Odds API bills for each one that exists — 2.59 came back per request when a
pick reads 1-2. `_markets_for_pick` now asks only for the pick's own market at
its own period, which makes the request list a *promise* about what the lookups
will consult. Break that promise in either direction and nothing errors:

  * omit a market a lookup reads  -> the price silently drops to no-match, or an
    exact_alt (10.6% of priced picks) degrades to proximity
  * include one no lookup reads   -> billed for it every request, forever

The alternate-market conditions are deliberately non-uniform per bet type
(spreads: game + MLB innings only; totals: also hockey periods; team_totals:
always; h2h: never), so this asserts them case by case rather than assuming.

Run: python scripts/test_market_narrowing.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds  # noqa: E402

# (label, bet_type, period, sport, description, markets the lookup will read)
CASES = [
    ("game ML",            "moneyline",  "game", "MLB",    "Yankees ML",              {"h2h"}),
    ("regulation NHL ML",  "moneyline",  "game", "NHL",    "Oilers 3-way regulation", {"h2h", "h2h_3_way"}),
    ("1H ML",              "moneyline",  "1h",   "NBA",    "Celtics 1H ML",           {"h2h_h1"}),
    ("game spread",        "spread",     "game", "NFL",    "Bills -3.5",              {"spreads", "alternate_spreads"}),
    ("1H spread",          "spread",     "1h",   "NBA",    "Celtics 1H -2.5",         {"spreads_h1"}),
    ("F5 spread (MLB)",    "spread",     "1h",   "MLB",    "F5 -0.5",                 {"spreads_1st_5_innings", "alternate_spreads_1st_5_innings"}),
    ("game total",         "total",      "game", "NBA",    "over 220.5",              {"totals", "alternate_totals"}),
    ("1H total",           "total",      "1h",   "WNBA",   "1H under 79.5",           {"totals_h1"}),
    ("P1 total (NHL)",     "total",      "1p",   "NHL",    "1st period over 1.5",     {"totals_p1", "alternate_totals_p1"}),
    ("F5 total (MLB)",     "total",      "1h",   "MLB",    "F5 under 4.5",            {"totals_1st_5_innings", "alternate_totals_1st_5_innings"}),
    ("team total",         "team_total", "game", "NBA",    "Hornets TT o117.5",       {"team_totals", "alternate_team_totals"}),
]


def lookup_markets(bet_type: str, period: str, sport: str, desc: str) -> set[str]:
    """Rebuild the market keys the real `_lookup_*` would consult, from the same
    helpers they use — not from a hand-copied list."""
    suffix = odds._get_period_suffix(period, sport)
    if bet_type == "moneyline":
        base = "h2h_3_way" if odds.is_regulation_ml(desc) else "h2h"
        got = {base + suffix}
        # a regulation ML still resolves through plain h2h when 3-way is absent
        if base == "h2h_3_way":
            got.add("h2h" + suffix)
        return got
    base = {"spread": "spreads", "total": "totals", "team_total": "team_totals"}[bet_type]
    got = {base + suffix}
    alt = odds._alt_market_for(base, suffix)
    if alt:
        got.add(alt)
    return got


def main() -> int:
    failures = []
    print(f"{'case':20s} {'requested':52s} reads")
    for label, bt, period, sport, desc, expected in CASES:
        pick = {"bet_type": bt, "period": period, "description": desc}
        odds.set_economy(False)
        requested = set(odds._markets_for_pick(pick, sport).split(","))
        reads = lookup_markets(bt, period, sport, desc)

        missing = reads - requested
        # h2h fallback for a 3-way ML is a tolerated extra read, not a promise
        missing.discard("h2h" if "h2h_3_way" in reads else "")
        unread = requested - reads

        status = "ok"
        if missing:
            failures.append(f"{label}: requests miss {sorted(missing)} that the lookup reads")
            status = "MISSING"
        if unread:
            failures.append(f"{label}: requests {sorted(unread)} that no lookup reads (billed for nothing)")
            status = "WASTE"
        print(f"{label:20s} {','.join(sorted(requested)):52s} {','.join(sorted(reads))}  [{status}]")

        # economy mode may drop the alternate, but never the main market
        odds.set_economy(True)
        econ = set(odds._markets_for_pick(pick, sport).split(","))
        odds.set_economy(False)
        main_mkt = min(reads, key=len)
        if main_mkt not in econ:
            failures.append(f"{label}: economy mode dropped the MAIN market {main_mkt}")

    print()
    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print(f"PASS — {len(CASES)} cases, requested set == markets read, economy keeps every main market")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
