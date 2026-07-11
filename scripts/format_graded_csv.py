"""
format_graded_csv.py — Convert graded CSV to the Sharp Syndicate spreadsheet format.

Reads BookitWithTrent_graded.csv and outputs BookitWithTrent_sheet.csv with columns:
  Game date, League, Play, Wagered Units, Bet type, Odds, W/L, Return, Position

Usage:
    python scripts/format_graded_csv.py
"""

import asyncio
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odds import fetch_odds

INPUT = os.path.join(os.path.dirname(__file__), "output", "BookitWithTrent_graded.csv")
OUTPUT = os.path.join(os.path.dirname(__file__), "output", "BookitWithTrent_sheet.csv")


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


async def run() -> None:
    with open(INPUT, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    out_rows = []
    api_hits = 0
    api_misses = 0

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

        # Fall back to Odds API for missing odds
        if not odds:
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
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    wins = sum(1 for r in out_rows if r["W/L"] == "win")
    losses = sum(1 for r in out_rows if r["W/L"] == "lose")
    pushes = sum(1 for r in out_rows if r["W/L"] == "push")
    total_return = sum(r["Return"] for r in out_rows if isinstance(r["Return"], (int, float)))
    with_odds = sum(1 for r in out_rows if r["Odds"])

    print(f"\nWrote {len(out_rows)} rows to {os.path.basename(OUTPUT)}")
    print(f"Record: {wins}W - {losses}L - {pushes}P")
    print(f"Total return: {total_return:+.2f}U")
    print(f"Odds: {with_odds}/{len(out_rows)} ({api_hits} from API, {with_odds - api_hits} from text, {api_misses} missed)")


if __name__ == "__main__":
    asyncio.run(run())
