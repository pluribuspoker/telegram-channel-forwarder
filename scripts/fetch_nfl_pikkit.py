#!/usr/bin/env python3
"""Capture due NFL Pikkit split snapshots."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from nfl_lines import GAME_HEADERS, SHEET_TABS, get_gspread_client
from nfl_pikkit import (
    PIKKIT_SNAPSHOTS_TAB,
    append_snapshot_rows,
    collect_due_snapshots,
    load_snapshot_rows,
    open_snapshot_worksheet,
)
from pikkit import fetch_events_for_date, fetch_splits


async def run(args: argparse.Namespace) -> int:
    credentials = os.environ.get("GOOGLE_CREDENTIALS", "")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID", "")
    if not credentials or not sheet_id:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required"
        )
    if not os.environ.get("PIKKIT_TOKEN", ""):
        raise RuntimeError("PIKKIT_TOKEN is required")

    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    games = spreadsheet.worksheet(SHEET_TABS["games"]).get_all_records(
        expected_headers=GAME_HEADERS,
        numericise_ignore=["all"],
    )
    worksheet = open_snapshot_worksheet(credentials, sheet_id)
    existing = load_snapshot_rows(worksheet)
    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else datetime.now(timezone.utc)
    )
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rows, outcomes = await collect_due_snapshots(
        games,
        existing,
        now,
        event_loader=fetch_events_for_date,
        split_loader=fetch_splits,
        target_event_id=args.target,
    )
    counts = Counter(item["status"] for item in outcomes)
    for item in outcomes:
        detail = f" ({item['error']})" if item.get("error") else ""
        print(
            f"{item['event_id']} {item['capture_kind']}: "
            f"{item['status']}{detail}"
        )
    if args.write:
        appended = append_snapshot_rows(worksheet, rows)
        print(f"Appended {appended} row(s) to {PIKKIT_SNAPSHOTS_TAB}.")
    else:
        print(f"Dry run: {len(rows)} row(s) ready; pass --write to append.")
    print(
        "Summary: "
        + ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
        if counts
        else "Summary: no captures due"
    )
    return 1 if counts.get("error") else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Append verified due snapshots. Without this, perform a dry run.",
    )
    parser.add_argument("--target", help="Limit collection to one NFL event id.")
    parser.add_argument(
        "--now",
        help="Override current time with an ISO timestamp for diagnostics.",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
