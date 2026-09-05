#!/usr/bin/env python3
"""Generate a versioned MOE opinion for one NFL game."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from moe import (
    build_divisional_input,
    build_schedule_input,
    configured_opinion_store,
    generate_opinion,
    load_expert,
)
from nfl_game_history import (
    GAME_HISTORY_HEADERS,
    GAME_HISTORY_TAB,
    build_game_history,
    fetch_regular_season_events,
)
from nfl_lines import GAME_HEADERS, get_gspread_client
from nfl_schedule import SCHEDULE_HEADERS, SCHEDULE_TAB


def _latest_alignment(history: list[dict]) -> list[dict]:
    alignment: dict[str, dict] = {}
    for row in sorted(history, key=lambda item: int(item["season"])):
        alignment[str(row["away_team"])] = {
            "team": str(row["away_team"]),
            "conference": str(row["away_conference"]),
            "division": str(row["away_division"]),
        }
        alignment[str(row["home_team"])] = {
            "team": str(row["home_team"]),
            "conference": str(row["home_conference"]),
            "division": str(row["home_division"]),
        }
    return list(alignment.values())


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--expert", default="schedule")
    parser.add_argument(
        "--model",
        help="Exact model ID. Defaults to the expert default, then MOE_MODEL.",
    )
    parser.add_argument(
        "--show-input",
        action="store_true",
        help="Print the enforced expert input package without calling a model.",
    )
    args = parser.parse_args()

    credentials = os.environ.get("GOOGLE_CREDENTIALS", "")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID", "")
    if not credentials or not sheet_id:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required"
        )
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    games = spreadsheet.worksheet("nfl_games").get_all_records(
        expected_headers=GAME_HEADERS
    )
    history = spreadsheet.worksheet(GAME_HISTORY_TAB).get_all_records(
        expected_headers=GAME_HISTORY_HEADERS
    )
    game = next(
        (
            row
            for row in games
            if str(row.get("event_id")) == str(args.event_id)
        ),
        None,
    )
    if game is None:
        raise ValueError(f"Unknown nfl_games event_id: {args.event_id}")
    expert = load_expert(args.expert)
    schedule: list[dict] | None = None
    current_results: list[dict] | None = None
    if expert["input_profile"] == "divisional":
        schedule = spreadsheet.worksheet(SCHEDULE_TAB).get_all_records(
            expected_headers=SCHEDULE_HEADERS
        )
        season = int(game["season"])
        current_events = fetch_regular_season_events(
            season, expected_games=None
        )
        current_results = build_game_history(
            {season: current_events},
            {season: _latest_alignment(history)},
            validate=False,
            require_complete_divisional_pairs=False,
        )
    if args.show_input:
        input_payload = (
            build_schedule_input(game, history)
            if expert["input_profile"] == "schedule_only"
            else build_divisional_input(
                game,
                history,
                schedule or [],
                current_results,
            )
        )
        print(
            json.dumps(
                input_payload,
                indent=2,
                sort_keys=True,
            )
        )
        return

    opinion = await generate_opinion(
        expert_id=args.expert,
        game=game,
        history=history,
        schedule=schedule,
        current_season_results=current_results,
        store=configured_opinion_store(),
        model=args.model,
        repair_attempts=(
            2 if int(expert["output_schema_version"]) in {3, 4} else 0
        ),
    )
    print(
        f"{opinion['expert_name']}: {opinion['predicted_winner']} | "
        f"score {opinion['predicted_away_score']}-"
        f"{opinion['predicted_home_score']} | "
        f"home {opinion['home_win_probability']:.0%} | "
        f"margin {opinion['expected_home_margin']:+.1f} | "
        f"{'★' * opinion['confidence_stars']}"
    )
    print(opinion["full_opinion"])
    print(f"Persisted opinion {opinion['opinion_id']} to moe_opinions.")


if __name__ == "__main__":
    asyncio.run(main())
