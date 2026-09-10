#!/usr/bin/env python3
"""Grade canonical celebrity NFL picks at their exact stated terms."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from celebrity_grades import (
    CelebrityPickGradeStore,
    build_celebrity_grade_rows,
    configured_celebrity_grade_store,
)
from celebrity_picks import CELEBRITY_HEADERS, CELEBRITY_TAB
from intake_bot import _celebrity_worksheet
from nfl_game_annotations import attach_game_annotations, load_game_annotations
from nfl_game_history import (
    GAME_HISTORY_HEADERS,
    GAME_HISTORY_TAB,
    build_game_history,
    fetch_regular_season_events,
)
from nfl_lines import LEAN_HEADERS, get_gspread_client
from scripts.generate_moe_opinion import _latest_alignment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--event-id", action="append", default=[])
    parser.add_argument("--database", type=Path)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    spreadsheet = get_gspread_client(
        os.environ["GOOGLE_CREDENTIALS"]
    ).open_by_key(os.environ["NFL_INTAKE_SHEET_ID"])
    history = spreadsheet.worksheet(GAME_HISTORY_TAB).get_all_records(
        expected_headers=GAME_HISTORY_HEADERS
    )
    annotation_rows = load_game_annotations(spreadsheet)
    events = fetch_regular_season_events(args.season, expected_games=None)
    finals = attach_game_annotations(
        build_game_history(
            {args.season: events},
            {args.season: _latest_alignment(history)},
            validate=False,
            require_complete_divisional_pairs=False,
        ),
        annotation_rows,
    )
    celebrity_rows = _celebrity_worksheet(spreadsheet).get_all_records(
        expected_headers=CELEBRITY_HEADERS
    )
    leans = spreadsheet.worksheet("nfl_leans").get_all_records(
        expected_headers=LEAN_HEADERS
    )
    graded_at = datetime.now(timezone.utc).isoformat()
    rows = build_celebrity_grade_rows(
        celebrity_rows,
        leans,
        history,
        preferred_finals=finals,
        season=args.season,
        event_ids=set(args.event_id) or None,
        graded_at_utc=graded_at,
    )
    inserted = 0
    if args.write:
        store = (
            CelebrityPickGradeStore(
                args.database.expanduser().resolve(),
                writable=True,
                initialize=True,
            )
            if args.database
            else configured_celebrity_grade_store(
                writable=True,
                initialize=True,
            )
        )
        inserted = store.append_rows(rows)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print(
            f"Celebrity grades: {len(rows)} gradeable latest picks"
            + (f", {inserted} appended" if args.write else "")
        )
        for row in rows:
            line = f" {row['line']}" if row["line"] else ""
            print(
                f"{row['celebrity_name']}: {row['direction']}{line} "
                f"{row['result']} ({row['final_away_score']}-"
                f"{row['final_home_score']})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
