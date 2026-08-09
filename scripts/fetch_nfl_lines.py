#!/usr/bin/env python3
"""Fetch BetOnline NFL lines and optionally update the intake workbook."""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_environment(root: Path) -> None:
    # Existing process values (including systemd overrides) always win.
    # Loading local first preserves its precedence for direct CLI runs.
    load_dotenv(root / ".env.local")
    load_dotenv(root / ".env")


_load_environment(ROOT)

from nfl_lines import (
    SHEET_TABS,
    fetch_all,
    format_summary,
    get_gspread_client,
    required_env,
    scheduled_poll_plan,
    write_to_sheets,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--season-type",
        choices=("regular", "preseason", "both"),
        default="both",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Update nfl_games and nfl_line_snapshots in Google Sheets.",
    )
    parser.add_argument(
        "--period-window-hours",
        type=int,
        default=0,
        help="Limit period-market checks by hours to kickoff; 0 checks every game.",
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Skip the Odds API call unless the workbook cadence says a game is due.",
    )
    args = parser.parse_args()

    api_key, credentials, sheet_id = required_env()
    season_types = (
        ["regular", "preseason"]
        if args.season_type == "both"
        else [args.season_type]
    )
    period_event_ids = None
    known_event_ids = None
    if args.scheduled:
        if not credentials:
            raise SystemExit("GOOGLE_CREDENTIALS is not set")
        if not sheet_id:
            raise SystemExit("NFL_INTAKE_SHEET_ID is not set")
        spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
        records = spreadsheet.worksheet(
            SHEET_TABS["games"]
        ).get_all_records()
        due, period_event_ids, known_event_ids = scheduled_poll_plan(
            records, datetime.now(timezone.utc)
        )
        if not due:
            print("No NFL games are due for polling; no API calls made.")
            return
        print(
            f"Scheduled poll due; checking period markets for "
            f"{len(period_event_ids)} known event(s)."
        )
    games = fetch_all(
        api_key,
        season_types,
        period_window_hours=args.period_window_hours,
        period_event_ids=period_event_ids,
        known_event_ids=known_event_ids,
    )
    print(format_summary(games))
    print(f"\nFetched {len(games)} upcoming BetOnline game(s).")

    if not args.write:
        print("Dry run only; pass --write to update Google Sheets.")
        return
    if not credentials:
        raise SystemExit("GOOGLE_CREDENTIALS is not set")
    if not sheet_id:
        raise SystemExit("NFL_INTAKE_SHEET_ID is not set")
    game_count, snapshot_count = write_to_sheets(
        games,
        sheet_id=sheet_id,
        credentials_b64=credentials,
    )
    print(
        f"Updated {game_count} nfl_games row(s); "
        f"appended {snapshot_count} snapshot row(s)."
    )


if __name__ == "__main__":
    main()
