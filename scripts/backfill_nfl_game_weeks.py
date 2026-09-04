#!/usr/bin/env python3
"""Backfill authoritative ESPN week numbers into nfl_games."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from nfl_lines import (
    GAME_HEADERS,
    fetch_espn_week_calendar,
    get_gspread_client,
    week_for_game,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write validated week numbers to the live nfl_games tab.",
    )
    args = parser.parse_args()

    credentials = os.environ["GOOGLE_CREDENTIALS"]
    sheet_id = os.environ["NFL_INTAKE_SHEET_ID"]
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    worksheet = spreadsheet.worksheet("nfl_games")
    rows = worksheet.get_all_records(expected_headers=GAME_HEADERS)
    calendars = {
        season: fetch_espn_week_calendar(season)
        for season in sorted({int(row["season"]) for row in rows})
    }
    updates = []
    counts: dict[tuple[str, int], int] = {}
    for row_number, row in enumerate(rows, start=2):
        season = int(row["season"])
        season_type = str(row["season_type"])
        week = week_for_game(
            commence_time=str(row["commence_time_utc"]),
            season_type=season_type,
            calendar=calendars[season],
        )
        counts[(season_type, week)] = counts.get((season_type, week), 0) + 1
        if str(row.get("week") or "") != str(week):
            updates.append(
                {
                    "range": f"D{row_number}",
                    "values": [[week]],
                }
            )
    for (season_type, week), count in sorted(counts.items()):
        print(f"{season_type} week {week}: {count} games")
    print(f"{len(updates)} of {len(rows)} rows require an update.")
    if not args.apply:
        print("Preview only; pass --apply to update nfl_games.")
        return
    if updates:
        worksheet.batch_update(updates, value_input_option="RAW")
    persisted = worksheet.get_all_records(expected_headers=GAME_HEADERS)
    if any(not str(row.get("week") or "").strip() for row in persisted):
        raise RuntimeError("nfl_games still contains blank week values")
    print(f"Validated authoritative weeks for {len(persisted)} nfl_games rows.")


if __name__ == "__main__":
    main()
