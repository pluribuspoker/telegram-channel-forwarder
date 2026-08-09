"""
format_graded_csv.py — Convert graded CSV to the Sharp Syndicate spreadsheet format.

Step 4 of the capper backfill pipeline:
    fetch_x_posts -> parse_posts_csv -> grade_csv -> format_graded_csv

Reads <account>_graded.csv and outputs <account>_sheet.csv with columns:
  Game date, League, Play, Wagered Units, Bet type, Odds, W/L, Return, Position

Usage:
    python scripts/format_graded_csv.py --account boyerBets_
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odds import (fetch_odds, set_economy as odds_set_economy,
                  quota_remaining as odds_quota_remaining, quota_used as odds_quota_used)
from scores import espn_closing_odds

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")


def _american_to_decimal(american: float) -> float:
    if american > 0:
        return round(american / 100 + 1, 2)
    else:
        return round(100 / abs(american) + 1, 2)


def _extract_odds_from_text(desc: str, line_val: str, bet_type: str) -> float | None:
    """Try to extract American odds from description text and convert to decimal."""
    # For moneyline, the line column often holds the American odds
    if bet_type == "moneyline" and line_val:
        try:
            v = float(line_val)
            if abs(v) >= 100:
                return _american_to_decimal(v)
        except ValueError:
            pass

    # Scan for any American odds token (+/-NNN, |NNN| >= 100).
    # Take the LAST match — earlier numbers are more likely spread lines.
    last = None
    for m in re.finditer(r'[+-]\d{3,}', desc):
        v = float(m.group())
        if abs(v) >= 100:
            last = v
    if last is not None:
        return _american_to_decimal(last)
    return None


def _map_bet_type(bet_type: str, prop_stat: str, sport: str, desc: str) -> str:
    if prop_stat == "BTTS":
        return "BTTS"
    if bet_type == "prop":
        return "PLAYER PROPS"
    if bet_type == "moneyline":
        if sport == "Soccer" and not re.search(r'(?i)to (advance|qualify)', desc):
            return "3W"
        return "MONEYLINE"
    if bet_type == "spread":
        return "SPREAD"
    if bet_type in ("total", "team_total"):
        return "TOTAL"
    if bet_type == "draw_no_bet":
        return "SPREAD"
    if bet_type == "double_chance":
        return "PARLAY"
    return bet_type.upper()


def _map_position(bet_type: str, prop_stat: str, direction: str,
                  line_val: str, odds: float | None) -> str:
    if prop_stat == "BTTS":
        return "BTTS"
    if bet_type == "prop":
        return "PROPS"
    if bet_type in ("total", "team_total"):
        if direction == "over":
            return "OVER"
        if direction == "under":
            return "UNDER"
        return "OVER"
    if bet_type == "spread":
        try:
            spread = float(line_val)
            return "DOG" if spread > 0 else "FAV"
        except (ValueError, TypeError):
            return "FAV"
    if bet_type == "moneyline":
        if odds and odds >= 2.0:
            return "DOG"
        return "FAV"
    if bet_type == "draw_no_bet":
        return "FAV"
    return "FAV"


def _row_to_pick(row: dict) -> dict:
    """Build a pick dict for fetch_odds."""
    teams = []
    if row.get("teams"):
        try:
            teams = json.loads(row["teams"])
        except json.JSONDecodeError:
            pass
    line = None
    if row.get("line"):
        try:
            line = float(row["line"])
        except ValueError:
            pass
    # For moneyline, the line column sometimes holds American odds, not a spread
    if row.get("bet_type") == "moneyline" and line and abs(line) >= 100:
        line = None
    return {
        "description": row.get("description", ""),
        "bet_type": row.get("bet_type", ""),
        "period": row.get("period", "game") or "game",
        "teams": teams,
        "player": row.get("player") or None,
        "prop_stat": row.get("prop_stat") or None,
        "line": line,
        "direction": row.get("direction") or None,
        "is_parlay_leg": False,
    }


# A spread or total is bought at roughly even money; a price far off that means
# the odds lookup found the wrong market or the wrong game. This is the only
# signal that a wrong date silently produced plausible-looking output: a 4/21
# Blazers +11.5 dated 4/22 came back at 1.22 (-455), which is not a real spread
# price. A win priced wrong changes the P&L, so those are flagged loudest.
_PLAUSIBLE = {
    "SPREAD": (1.65, 2.35),
    "TOTAL": (1.65, 2.35),
    "MONEYLINE": (1.15, 4.00),
}


def _warn_implausible_odds(rows: list[dict]) -> None:
    bad = []
    for r in rows:
        lo_hi = _PLAUSIBLE.get(r["Bet type"])
        if not lo_hi or not r["Odds"]:
            continue
        lo, hi = lo_hi
        if not (lo <= float(r["Odds"]) <= hi):
            bad.append(r)
    if not bad:
        return
    affects_pnl = [r for r in bad if r["W/L"] == "win"]
    print(f"\n⚠ {len(bad)} row(s) with an implausible price "
          f"({len(affects_pnl)} on WINS, which changes the P&L):")
    for r in sorted(bad, key=lambda x: x["W/L"] != "win"):
        flag = "  <-- WIN, affects P&L" if r["W/L"] == "win" else ""
        print(f"    {r['Game date']:10s} {r['League']:6s} {r['Bet type']:9s} "
              f"{r['Odds']:>5}  {r['Play'][:44]}{flag}")
    print("    Check the game date first — a date that is off by one is the "
          "usual cause.")


# Historical odds cost 10 per region per market. In economy mode that is one
# market in one region, so ~10 credits per priced row; full mode averaged 48.
_CREDITS_PER_ROW_ECONOMY = 10
_CREDITS_PER_ROW_FULL = 48


async def _preflight_quota(rows: list[dict], full_fidelity: bool, force: bool) -> bool:
    """Refuse to start a backfill that would eat the month's quota.

    This stage calls the HISTORICAL endpoint once per row with no odds in the
    text, and a season's worth of picks is 15,000-20,000 credits against a
    20,000/month plan — which is exactly how the quota hit zero on 2026-08-08
    with no warning. The probe itself is free.
    """
    need = sum(1 for r in rows
               if r.get("grade") in ("WIN", "LOSS", "PUSH")
               and not _extract_odds_from_text(r.get("description", ""), r.get("line", ""),
                                               r.get("bet_type", "")))
    per = _CREDITS_PER_ROW_FULL if full_fidelity else _CREDITS_PER_ROW_ECONOMY
    est = need * per
    remaining = await odds_quota_remaining()
    mode = "full-fidelity" if full_fidelity else "economy (1 market, us only)"
    print(f"Odds API pre-flight: {need} row(s) need a price, {mode}, "
          f"~{per} credits each -> ~{est} credits")
    if remaining is None:
        print("  quota unknown (no API key or probe failed) — continuing")
        return True
    print(f"  quota remaining: {remaining}")
    if est <= remaining:
        return True
    print(f"\n⚠ This run would need ~{est} credits but only {remaining} remain.")
    if force:
        print("  --force given, continuing anyway (prices will drop to the -110 "
              "default once the quota runs out).")
        return True
    print("  Refusing to start. Options: wait for the monthly reset (it renews on "
          "your subscription anniversary), narrow the run with --since/--limit "
          "upstream, or pass --force to accept partial pricing.")
    return False


async def run(account: str, full_fidelity: bool = False, force: bool = False,
              use_odds_api: bool = False) -> None:
    input_csv = os.path.join(OUT_DIR, f"{account}_graded.csv")
    output_csv = os.path.join(OUT_DIR, f"{account}_sheet.csv")

    with open(input_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if use_odds_api:
        # Backfills don't need every alternate line and every book — a miss just
        # falls back to -110 — so they fetch the one market the pick reads.
        if not full_fidelity:
            odds_set_economy(True)
        if not await _preflight_quota(rows, full_fidelity, force):
            return
    else:
        print("Odds: text -> ESPN closing line (free). Odds API disabled "
              "(--use-odds-api to enable).")

    out_rows = []
    api_hits = 0
    api_misses = 0
    espn_hits = 0
    text_hits = 0
    defaulted = 0

    for i, row in enumerate(rows):
        grade = row.get("grade", "")
        if grade not in ("WIN", "LOSS", "PUSH"):
            continue

        iso = row["date"][:10]
        y, m, d = iso.split("-")
        game_date = f"{int(m)}/{int(d)}/{y}"

        league = row.get("sport", "").upper()
        desc = row.get("description", "")
        bet_type = row.get("bet_type", "")
        prop_stat = row.get("prop_stat", "")
        direction = row.get("direction", "")
        line_val = row.get("line", "")

        # Try text extraction first (free, no API call)
        odds = _extract_odds_from_text(desc, line_val, bet_type)
        odds_source = "text" if odds else None
        if odds:
            text_hits += 1

        # Then ESPN's closing line — also free, no key and no quota. Covers
        # game-level ML/spread/total, which is most of a capper's book.
        if not odds:
            pick = _row_to_pick(row)
            espn = await espn_closing_odds(row.get("sport", ""), iso, pick)
            if espn:
                odds = _american_to_decimal(espn["odds"])
                odds_source = "espn"
                espn_hits += 1

        # The Odds API is opt-in: its historical endpoint costs 10 per region
        # per market and is what drained the month's quota on 2026-08-08.
        if not odds and use_odds_api:
            pick = _row_to_pick(row)
            result = await fetch_odds(row.get("sport", ""), iso, pick)
            # Only accept exact/exact_alt matches — proximity adjustments
            # aren't calibrated for soccer and produce bad values.
            if result.found and result.match_type in ("exact", "exact_alt"):
                odds = _american_to_decimal(result.odds)
                odds_source = "api"
                api_hits += 1
            else:
                api_misses += 1

        # Default missing odds to -110 (1.91 decimal)
        if not odds:
            odds = _american_to_decimal(-110)
            defaulted += 1

        mapped_type = _map_bet_type(bet_type, prop_stat, row.get("sport", ""), desc)
        position = _map_position(bet_type, prop_stat, direction, line_val, odds)

        wl = "win" if grade == "WIN" else ("lose" if grade == "LOSS" else "push")
        units = 1

        if odds:
            if grade == "WIN":
                ret = round((odds - 1) * units, 2)
            elif grade == "LOSS":
                ret = -units
            else:
                ret = 0
        else:
            ret = units if grade == "WIN" else (-units if grade == "LOSS" else 0)

        label = f"  [{i+1}] {odds_source or 'MISS':4s}  {odds or '':>6}  {desc[:55]}"
        print(label)

        out_rows.append({
            "Game date": game_date,
            "League": league,
            "Play": desc,
            "Wagered Units": units,
            "Bet type": mapped_type,
            "Odds": odds or "",
            "W/L": wl,
            "Return": ret,
            "Position": position,
        })

    fieldnames = ["Game date", "League", "Play", "Wagered Units", "Bet type",
                  "Odds", "W/L", "Return", "Position"]
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    _warn_implausible_odds(out_rows)

    wins = sum(1 for r in out_rows if r["W/L"] == "win")
    losses = sum(1 for r in out_rows if r["W/L"] == "lose")
    pushes = sum(1 for r in out_rows if r["W/L"] == "push")
    total_return = sum(r["Return"] for r in out_rows if isinstance(r["Return"], (int, float)))
    with_odds = sum(1 for r in out_rows if r["Odds"])

    print(f"\nWrote {len(out_rows)} rows to {os.path.basename(output_csv)}")
    print(f"Record: {wins}W - {losses}L - {pushes}P")
    print(f"Total return: {total_return:+.2f}U")
    # Counted, never derived: a residual silently folds the -110 defaults into
    # "from text" and a coverage gap then reads as full coverage.
    priced = text_hits + espn_hits + api_hits
    print(f"Odds: {priced}/{len(out_rows)} priced "
          f"({text_hits} text, {espn_hits} ESPN, {api_hits} Odds API); "
          f"{defaulted} defaulted to -110")
    print(f"Odds API credits used this run: {odds_quota_used()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True, help="X handle, e.g. boyerBets_")
    parser.add_argument("--full-fidelity", action="store_true",
                        help="Fetch alternate lines too (~5x the quota cost). Default is "
                             "economy: one market per pick, misses fall back to -110.")
    parser.add_argument("--force", action="store_true",
                        help="Run even if the estimated cost exceeds the remaining quota.")
    parser.add_argument("--use-odds-api", action="store_true",
                        help="Also use the paid Odds API for gaps ESPN can't fill. "
                             "Off by default — backfills cost zero quota.")
    args = parser.parse_args()
    asyncio.run(run(args.account, full_fidelity=args.full_fidelity, force=args.force,
                    use_odds_api=args.use_odds_api))
