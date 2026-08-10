#!/usr/bin/env python3
"""Create and seed NFL season-win prediction tabs in the intake workbook."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from nfl_lines import get_gspread_client
from nfl_win_predictions import (
    TAB_HEADERS,
    build_latest_prediction_rows,
    build_rank_benchmarks,
    build_team_history,
    build_win_total_rows,
    ensure_worksheet,
    fetch_standings,
    replace_rows,
)

WIN_TOTALS = {
    "New England Patriots": 9.5,
    "Seattle Seahawks": 10.5,
    "Los Angeles Rams": 11.5,
    "San Francisco 49ers": 10.5,
    "Atlanta Falcons": 7.5,
    "Baltimore Ravens": 11.5,
    "Buffalo Bills": 10.5,
    "Carolina Panthers": 7.5,
    "Chicago Bears": 9.5,
    "Cincinnati Bengals": 10.5,
    "Cleveland Browns": 5.5,
    "Detroit Lions": 10.5,
    "Houston Texans": 9.5,
    "Indianapolis Colts": 7.5,
    "Jacksonville Jaguars": 9.5,
    "New Orleans Saints": 7.5,
    "New York Jets": 5.5,
    "Pittsburgh Steelers": 7.5,
    "Tampa Bay Buccaneers": 8.5,
    "Tennessee Titans": 6.5,
    "Arizona Cardinals": 4.5,
    "Green Bay Packers": 9.5,
    "Las Vegas Raiders": 5.5,
    "Los Angeles Chargers": 9.5,
    "Miami Dolphins": 4.5,
    "Minnesota Vikings": 8.5,
    "Philadelphia Eagles": 9.5,
    "Washington Commanders": 8.5,
    "Dallas Cowboys": 9.5,
    "New York Giants": 7.5,
    "Denver Broncos": 9.5,
    "Kansas City Chiefs": 10.5,
}

CAPTURED_AT = datetime.fromisoformat("2026-08-09T23:34:34-04:00")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create/update the live workbook. Without this, print a preview.",
    )
    args = parser.parse_args()

    standings = {
        season: fetch_standings(season)
        for season in (2023, 2024, 2025)
    }
    history = build_team_history(standings)
    benchmarks = build_rank_benchmarks(history)
    totals = build_win_total_rows(
        WIN_TOTALS,
        season=2026,
        captured_at=CAPTURED_AT,
        source="user_paste",
    )

    print(
        f"Prepared {len(totals)} totals, {len(history)} team-season rows, "
        f"and {len(benchmarks)} rank benchmarks."
    )
    if not args.apply:
        print("Preview only; pass --apply to update the workbook.")
        return

    credentials = os.environ.get("GOOGLE_CREDENTIALS", "")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID", "")
    if not credentials or not sheet_id:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required"
        )
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    worksheets = {
        title: ensure_worksheet(spreadsheet, title, headers)
        for title, headers in TAB_HEADERS.items()
    }

    replace_rows(
        worksheets["nfl_win_totals"],
        TAB_HEADERS["nfl_win_totals"],
        totals,
    )
    replace_rows(
        worksheets["nfl_team_history"],
        TAB_HEADERS["nfl_team_history"],
        history,
    )
    replace_rows(
        worksheets["nfl_rank_benchmarks"],
        TAB_HEADERS["nfl_rank_benchmarks"],
        benchmarks,
    )

    predictions = worksheets["nfl_win_predictions"].get_all_records(
        expected_headers=TAB_HEADERS["nfl_win_predictions"]
    )
    latest = build_latest_prediction_rows(predictions, totals)
    replace_rows(
        worksheets["nfl_win_predictions_latest"],
        TAB_HEADERS["nfl_win_predictions_latest"],
        latest,
    )
    print(
        "Updated nfl_win_totals, nfl_team_history, nfl_rank_benchmarks, "
        "and nfl_win_predictions_latest; preserved nfl_win_predictions."
    )


if __name__ == "__main__":
    main()
