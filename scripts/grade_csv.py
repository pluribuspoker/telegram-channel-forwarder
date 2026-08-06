"""
grade_csv.py — Grade picks from a parsed CSV using the existing grading pipeline.

Step 3 of the capper backfill pipeline:
    fetch_x_posts -> parse_posts_csv -> grade_csv -> format_graded_csv

Usage:
    python scripts/grade_csv.py --account boyerBets_              # grade every sport
    python scripts/grade_csv.py --account boyerBets_ --sport NBA  # one sport only
    python scripts/grade_csv.py --account boyerBets_ --limit 5    # first 5 matching rows

Only rows whose parse `category` is in GRADEABLE_CATEGORIES (free PODs) are graded.
Widen with --categories pod,secondary,max or --categories all.
"""

import argparse
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
from scores import ESPN_LEAGUES, fetch_espn
from scripts.parse_posts_csv import GRADEABLE_CATEGORIES

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")

# ESPN scoreboard fetches are shared across picks, so grading runs concurrently.
CONCURRENCY = 6


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


async def _scoreboard_for(sport: str, date: str, cache: dict, lock: asyncio.Lock):
    """Fetch (and cache) the ESPN scoreboard for a sport/date.

    build_context returns CONTEXT_ESPN_ERROR for every ESPN-league sport when the
    scoreboard is None, so this must be populated or NBA/MLB/NFL/NHL/UFC rows all
    grade as UNKNOWN. Sports with their own providers (Soccer, Tennis, Boxing,
    KBO, CFL) are handled inside build_context and need no scoreboard.
    """
    if sport not in ESPN_LEAGUES:
        return None
    key = (sport, date)
    async with lock:
        if key not in cache:
            cache[key] = await fetch_espn(sport, date)
    return cache[key]


async def grade_row(row: dict, scoreboard_cache: dict, summary_cache: dict,
                    sb_lock: asyncio.Lock) -> tuple[str, str]:
    """Return (grade, calc) for one CSV row."""
    pick = _row_to_pick(row)
    if not pick["teams"] and not pick["player"]:
        return "SKIP", "no teams"

    sport = row.get("sport", "")
    date = row["date"][:10]

    scoreboard = await _scoreboard_for(sport, date, scoreboard_cache, sb_lock)

    context, game_date = await build_context(
        sport, date, pick, scoreboard, summary_cache
    )

    if context in (CONTEXT_SKIP, CONTEXT_ESPN_ERROR):
        return "UNKNOWN", "no game data"
    if context == CONTEXT_PENDING:
        return "PENDING", ""

    return await claude_grade(
        pick["description"], game_date, context,
        pick["bet_type"], pick.get("prop_stat") or "",
    )


async def run(account: str, sport_filter: str | None = None,
              limit: int | None = None,
              categories: set[str] | None = None) -> None:
    input_csv = os.path.join(OUT_DIR, f"{account}_parsed.csv")
    output_csv = os.path.join(OUT_DIR, f"{account}_graded.csv")

    with open(input_csv, newline="", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    categories = categories or GRADEABLE_CATEGORIES
    rows = all_rows
    if "all" not in categories and any(r.get("category") for r in all_rows):
        dropped = len(rows)
        rows = [r for r in rows if r.get("category", "") in categories]
        print(f"Category filter {sorted(categories)}: kept {len(rows)}, "
              f"dropped {dropped - len(rows)}")
    if sport_filter:
        rows = [r for r in rows if r.get("sport", "") == sport_filter]
    if limit:
        rows = rows[:limit]

    label = sport_filter or "all-sport"
    print(f"Grading {len(rows)} {label} picks from {os.path.basename(input_csv)}")
    print("=" * 72)

    scoreboard_cache: dict = {}
    summary_cache: dict = {}
    sb_lock = asyncio.Lock()
    sem = asyncio.Semaphore(CONCURRENCY)
    results: list[dict | None] = [None] * len(rows)
    done = 0

    async def process(i: int, row: dict):
        nonlocal done
        async with sem:
            try:
                grade, calc = await grade_row(row, scoreboard_cache, summary_cache, sb_lock)
            except Exception as e:
                grade, calc = "ERROR", f"{type(e).__name__}: {e}"
        results[i] = {**row, "grade": grade, "calc": calc}
        done += 1
        print(f"  [{done}/{len(rows)}] {grade:7s} {row.get('sport',''):7s} "
              f"{row.get('description','')[:48]:48s} | {calc[:36]}")

    await asyncio.gather(*(process(i, row) for i, row in enumerate(rows)))
    out = [r for r in results if r]

    fieldnames = list(all_rows[0].keys()) if all_rows else []
    fieldnames += ["grade", "calc"]
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out)

    graded = [r for r in out if r["grade"] in ("WIN", "LOSS", "PUSH")]
    wins = sum(1 for r in graded if r["grade"] == "WIN")
    losses = sum(1 for r in graded if r["grade"] == "LOSS")
    pushes = sum(1 for r in graded if r["grade"] == "PUSH")
    pending = sum(1 for r in out if r["grade"] == "PENDING")
    unknown = sum(1 for r in out if r["grade"] in ("UNKNOWN", "SKIP", "ERROR"))

    print(f"\n{'=' * 72}")
    print(f"Results: {wins}W - {losses}L - {pushes}P  |  pending: {pending}  |  unknown/skip: {unknown}")
    if graded:
        decided = wins + losses
        if decided:
            print(f"Win rate: {round(100 * wins / decided, 1)}% ({wins}/{decided})")

    # Per-sport coverage — surfaces a sport whose grading silently isn't working.
    by_sport: dict[str, list[str]] = {}
    for r in out:
        by_sport.setdefault(r.get("sport", "?"), []).append(r["grade"])
    print("\nBy sport:")
    for sport, grades in sorted(by_sport.items(), key=lambda kv: -len(kv[1])):
        ok = sum(1 for g in grades if g in ("WIN", "LOSS", "PUSH"))
        print(f"  {sport:10s} {ok}/{len(grades)} graded")

    print(f"\nCost: {fmt_cost(usage_cost())}")
    print(f"Output: {output_csv}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True, help="X handle, e.g. boyerBets_")
    parser.add_argument("--sport", default=None, help="Only grade this sport (default: all)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--categories",
        default=",".join(sorted(GRADEABLE_CATEGORIES)),
        help='Comma-separated parse categories to grade, or "all" '
             f'(default: {",".join(sorted(GRADEABLE_CATEGORIES))})',
    )
    args = parser.parse_args()
    asyncio.run(run(args.account, sport_filter=args.sport, limit=args.limit,
                    categories={c.strip() for c in args.categories.split(",") if c.strip()}))
