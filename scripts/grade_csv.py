"""
grade_csv.py — Grade picks from a parsed CSV using the existing grading pipeline.

Usage:
    python scripts/grade_csv.py                          # grade all Soccer rows
    python scripts/grade_csv.py --sport NBA              # grade NBA rows
    python scripts/grade_csv.py --limit 5                # grade first 5 matching rows
"""

import asyncio
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai import (
    build_context,
    claude_grade,
    CONTEXT_SKIP,
    CONTEXT_ESPN_ERROR,
    CONTEXT_PENDING,
    usage_cost,
    fmt_cost,
)

INPUT_CSV = os.path.join(os.path.dirname(__file__), "output", "BookitWithTrent_parsed.csv")
OUTPUT_CSV = os.path.join(os.path.dirname(__file__), "output", "BookitWithTrent_graded.csv")


def _parse_line(val: str) -> float | None:
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _row_to_pick(row: dict) -> dict:
    """Convert a CSV row into the pick dict expected by build_context / claude_grade."""
    teams = []
    if row.get("teams"):
        try:
            teams = json.loads(row["teams"])
        except json.JSONDecodeError:
            pass

    return {
        "description": row.get("description", ""),
        "bet_type": row.get("bet_type", ""),
        "period": row.get("period", "game") or "game",
        "teams": teams,
        "player": row.get("player") or None,
        "prop_stat": row.get("prop_stat") or None,
        "line": _parse_line(row.get("line", "")),
        "direction": row.get("direction") or None,
        "is_parlay_leg": False,
    }


async def run(sport_filter: str = "Soccer", limit: int | None = None) -> None:
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    rows = [r for r in all_rows if r.get("sport", "") == sport_filter]
    if limit:
        rows = rows[:limit]

    print(f"Grading {len(rows)} {sport_filter} picks from {os.path.basename(INPUT_CSV)}")
    print("=" * 72)

    summary_cache: dict = {}
    results: list[dict] = []

    for i, row in enumerate(rows, 1):
        pick = _row_to_pick(row)
        date = row["date"][:10]
        desc = pick["description"]
        bet_type = pick["bet_type"]
        prop_stat = pick.get("prop_stat") or ""

        if not pick["teams"]:
            print(f"  [{i}/{len(rows)}] SKIP (no teams)  {desc[:60]}")
            results.append({**row, "grade": "SKIP", "calc": "no teams"})
            continue

        # build_context for Soccer doesn't need a scoreboard arg
        context, game_date = await build_context(
            sport_filter, date, pick, None, summary_cache
        )

        if context in (CONTEXT_SKIP, CONTEXT_ESPN_ERROR):
            grade, calc = "UNKNOWN", "no game data"
            print(f"  [{i}/{len(rows)}] UNKNOWN (no data)  {desc[:60]}")
        elif context == CONTEXT_PENDING:
            grade, calc = "PENDING", ""
            print(f"  [{i}/{len(rows)}] PENDING  {desc[:60]}")
        else:
            grade, calc = await claude_grade(desc, game_date, context, bet_type, prop_stat)
            print(f"  [{i}/{len(rows)}] {grade:7s}  {desc[:50]}  |  {calc[:40]}")

        results.append({**row, "grade": grade, "calc": calc})

    # Write output CSV
    fieldnames = list(all_rows[0].keys()) if all_rows else list(rows[0].keys())
    fieldnames += ["grade", "calc"]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # Summary
    graded = [r for r in results if r["grade"] in ("WIN", "LOSS", "PUSH")]
    wins = sum(1 for r in graded if r["grade"] == "WIN")
    losses = sum(1 for r in graded if r["grade"] == "LOSS")
    pushes = sum(1 for r in graded if r["grade"] == "PUSH")
    pending = sum(1 for r in results if r["grade"] == "PENDING")
    unknown = sum(1 for r in results if r["grade"] in ("UNKNOWN", "SKIP"))

    print(f"\n{'=' * 72}")
    print(f"Results: {wins}W - {losses}L - {pushes}P  |  pending: {pending}  |  unknown/skip: {unknown}")
    if graded:
        pct = round(100 * wins / len(graded), 1)
        print(f"Win rate: {pct}% ({wins}/{len(graded)})")
    print(f"Cost: {fmt_cost(usage_cost())}")
    print(f"Output: {OUTPUT_CSV}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", default="Soccer")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(run(sport_filter=args.sport, limit=args.limit))
