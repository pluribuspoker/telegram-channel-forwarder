#!/usr/bin/env python3
"""Create or validate the NFL Pikkit snapshots worksheet."""

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

from nfl_lines import get_gspread_client
from nfl_pikkit import (
    PIKKIT_SNAPSHOT_HEADERS,
    PIKKIT_SNAPSHOTS_TAB,
    ensure_snapshot_worksheet,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create the worksheet when absent. Without this, print the schema.",
    )
    args = parser.parse_args()
    print(
        f"{PIKKIT_SNAPSHOTS_TAB}: {len(PIKKIT_SNAPSHOT_HEADERS)} columns"
    )
    if not args.apply:
        print("Preview only; pass --apply to create or validate the worksheet.")
        return
    credentials = os.environ.get("GOOGLE_CREDENTIALS", "")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID", "")
    if not credentials or not sheet_id:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required"
        )
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    ensure_snapshot_worksheet(spreadsheet)
    print(f"{PIKKIT_SNAPSHOTS_TAB} is ready.")


if __name__ == "__main__":
    main()
