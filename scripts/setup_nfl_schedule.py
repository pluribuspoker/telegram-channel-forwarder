#!/usr/bin/env python3
"""Create or replace the complete authoritative NFL schedule tab."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from nfl_lines import get_gspread_client
from nfl_schedule import (
    SCHEDULE_HEADERS,
    SCHEDULE_TAB,
    fetch_regular_season_schedule,
    validate_schedule,
)
from nfl_win_predictions import ensure_worksheet, replace_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Replace the live nfl_schedule tab after validation.",
    )
    args = parser.parse_args()

    rows = fetch_regular_season_schedule(args.season)
    counts = Counter(int(row["week"]) for row in rows)
    print(
        f"Prepared {len(rows)} games for {args.season}; "
        f"weeks={min(counts)}-{max(counts)}."
    )
    print(
        "Week counts: "
        + ", ".join(
            f"{week}={counts[week]}" for week in sorted(counts)
        )
    )
    if not args.apply:
        print("Preview only; pass --apply to replace nfl_schedule.")
        return

    credentials = os.environ["GOOGLE_CREDENTIALS"]
    sheet_id = os.environ["NFL_INTAKE_SHEET_ID"]
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    worksheet = ensure_worksheet(
        spreadsheet,
        SCHEDULE_TAB,
        SCHEDULE_HEADERS,
    )
    worksheet.resize(rows=len(rows) + 1, cols=len(SCHEDULE_HEADERS))
    replace_rows(worksheet, SCHEDULE_HEADERS, rows)
    persisted = worksheet.get_all_records(
        expected_headers=SCHEDULE_HEADERS
    )
    validate_schedule(persisted, season=args.season)
    print(
        f"Re-read and validated {len(persisted)} rows from {SCHEDULE_TAB}."
    )


if __name__ == "__main__":
    main()

