#!/usr/bin/env python3
"""Backfill completed NFL regular-season games into the intake workbook."""

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

from nfl_game_history import (
    GAME_HISTORY_HEADERS,
    GAME_HISTORY_TAB,
    build_game_history,
    fetch_regular_season_events,
    summarize_game_history,
    validate_game_history,
)
from nfl_lines import get_gspread_client
from nfl_win_predictions import (
    ensure_worksheet,
    fetch_standings,
    replace_rows,
)

BACKFILL_SEASONS = (2023, 2024, 2025)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seasons",
        nargs="+",
        type=int,
        default=list(BACKFILL_SEASONS),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Replace the nfl_game_history tab after validation.",
    )
    args = parser.parse_args()
    seasons = sorted(set(args.seasons))
    if args.apply and tuple(seasons) != BACKFILL_SEASONS:
        raise ValueError(
            "--apply replaces the whole tab and therefore requires exactly "
            f"these seasons: {', '.join(map(str, BACKFILL_SEASONS))}"
        )
    events = {
        season: fetch_regular_season_events(season)
        for season in seasons
    }
    standings = {
        season: fetch_standings(season)
        for season in seasons
    }
    rows = build_game_history(events, standings)
    for summary in summarize_game_history(rows):
        print(
            f"{summary['season']}: {summary['games']} games | "
            f"division={summary['division']} "
            f"conference={summary['conference']} "
            f"non_conference={summary['non_conference']} | "
            f"home={summary['home_wins']}-"
            f"{summary['home_losses']}-{summary['home_ties']} | "
            f"week_1_division="
            f"{summary['week_1_division_home_record']} "
            f"({summary['week_1_division_games']} games)"
        )
    if not args.apply:
        print(
            f"Prepared {len(rows)} rows. Preview only; pass --apply "
            f"to replace {GAME_HISTORY_TAB}."
        )
        return

    credentials = os.environ.get("GOOGLE_CREDENTIALS", "")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID", "")
    if not credentials or not sheet_id:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required"
        )
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    worksheet = ensure_worksheet(
        spreadsheet,
        GAME_HISTORY_TAB,
        GAME_HISTORY_HEADERS,
    )
    worksheet.resize(rows=len(rows) + 1, cols=len(GAME_HISTORY_HEADERS))
    replace_rows(worksheet, GAME_HISTORY_HEADERS, rows)
    persisted = worksheet.get_all_records(
        expected_headers=GAME_HISTORY_HEADERS
    )
    validate_game_history(
        persisted,
        expected_seasons=set(seasons),
    )
    print(
        f"Re-read and validated {len(persisted)} rows from "
        f"{GAME_HISTORY_TAB}."
    )


if __name__ == "__main__":
    main()
