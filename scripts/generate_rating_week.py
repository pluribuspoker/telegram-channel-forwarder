#!/usr/bin/env python3
"""Persist one ``rating_elo`` opinion per upcoming game of an NFL week.

The rating voice is arithmetic on the committed Elo prior and this season's
finals, so a week is generated in one command instead of sixteen::

    python scripts/generate_rating_week.py --season 2026 --week 3 [--dry-run]

For every ``nfl_games`` row of that season and week whose status is
``upcoming`` it builds the rating input and persists a row through
``generate_opinion`` on the deterministic backend -- unless a valid pending
or approved ``rating_elo`` row already carries the same input hash. New
finals change the input (and its hash), so a later run after more games are
final adds a fresher row for the same game; the earlier one keeps its
status.

The rating voice is reviewed by validation (registry ``review: validation``,
decided 2026-09-07): every row it persists is approved at generation,
hash-bound, because the response is arithmetic checked against the input's
own estimate. After the week's games, this script also approves the week's
valid ``rating_elo`` rows that are still pending -- rows persisted before the
policy existed, or by a single-game run -- through the store's hash-checked
review, as ``validation``. No human step remains for this expert; every
other expert keeps its human gate.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from moe import (
    MoeOpinionStore,
    configured_opinion_store,
    generate_opinion,
    load_expert,
)
from moe_god import (
    DETERMINISTIC_BACKEND,
    VALIDATION_REVIEW_NOTE,
    VALIDATION_REVIEWER,
    canonical_json,
    review_policy,
    sha256_text,
)
from moe_rating import RATING_EXPERT_ID, build_rating_input
from nfl_game_history import GAME_HISTORY_HEADERS, GAME_HISTORY_TAB
from nfl_lines import GAME_HEADERS, get_gspread_client
from scripts.generate_moe_opinion import current_season_finals
from scripts.review_moe_opinion import approve_rows, week_rows


def describe_game(game: dict[str, Any]) -> str:
    return f"{game['away_team']} @ {game['home_team']} ({game.get('commence_time_et') or game['commence_time_utc']})"


def week_games(
    games: Iterable[dict[str, Any]], *, season: int, week: int
) -> list[dict[str, Any]]:
    """Upcoming games of one season and week, in kickoff order."""
    selected = [
        game
        for game in games
        if str(game.get("status") or "") == "upcoming"
        and str(game.get("season") or "").strip()
        and int(game["season"]) == int(season)
        and str(game.get("week") or "").strip()
        and int(game["week"]) == int(week)
    ]
    selected.sort(
        key=lambda game: (str(game["commence_time_utc"]), str(game["home_team"]))
    )
    return selected


def existing_row(
    rows: Iterable[dict[str, Any]], *, event_id: str, input_sha256: str
) -> dict[str, Any] | None:
    """A valid pending or approved rating row for this event on this exact
    input, if one exists."""
    for row in rows:
        if (
            str(row.get("expert_id") or "") == RATING_EXPERT_ID
            and str(row.get("event_id") or "") == str(event_id)
            and str(row.get("generation_status") or "") == "valid"
            and str(row.get("review_status") or "") in {"pending", "approved"}
            and str(row.get("input_sha256") or "") == input_sha256
        ):
            return row
    return None


async def generate_week(
    *,
    games: Iterable[dict[str, Any]],
    opinion_rows: Iterable[dict[str, Any]],
    finals: Iterable[dict[str, Any]],
    store: MoeOpinionStore,
    season: int,
    week: int,
    history: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """One pass over the week; every side effect goes through ``store``."""
    opinion_rows = list(opinion_rows)
    finals = list(finals)
    summary: dict[str, list[dict[str, Any]]] = {
        "persisted": [],
        "up_to_date": [],
        "approved_earlier": [],
        "failed": [],
    }
    prefix = "dry run: " if dry_run else ""
    validation = review_policy(load_expert(RATING_EXPERT_ID)) == "validation"
    for game in week_games(games, season=season, week=week):
        event_id = str(game["event_id"])
        try:
            payload = build_rating_input(game, finals)
        except ValueError as exc:
            print(f"{prefix}{describe_game(game)}: input could not be built ({exc})")
            summary["failed"].append({"event_id": event_id, "error": str(exc)})
            continue
        digest = sha256_text(canonical_json(payload))
        current = existing_row(opinion_rows, event_id=event_id, input_sha256=digest)
        if current is not None:
            print(
                f"{prefix}{describe_game(game)}: up to date, "
                f"{current.get('review_status')} row {current.get('opinion_id')} "
                f"carries input {digest[:12]}"
            )
            summary["up_to_date"].append(
                {"event_id": event_id, "opinion_id": str(current.get("opinion_id"))}
            )
            continue
        estimate = payload["estimate"]
        plan = (
            f"{estimate['predicted_winner']} {estimate['predicted_away_score']}-"
            f"{estimate['predicted_home_score']}, p(home) "
            f"{estimate['home_win_probability']:.3f}, margin "
            f"{estimate['expected_home_margin']:+.1f}, "
            f"{'★' * int(estimate['confidence_stars'])}; "
            f"{payload['season']['finals_applied']} finals applied"
        )
        if dry_run:
            print(f"dry run: {describe_game(game)}: would persist {plan} (input {digest[:12]})")
            summary["persisted"].append({"event_id": event_id, "dry_run": True})
            continue
        try:
            row = await generate_opinion(
                expert_id=RATING_EXPERT_ID,
                game=game,
                history=history or [],
                current_season_results=finals,
                store=store,
                generation_backend=DETERMINISTIC_BACKEND,
            )
        except ValueError as exc:
            print(f"{describe_game(game)}: failed validation ({exc}); audit row persisted")
            summary["failed"].append({"event_id": event_id, "error": str(exc)})
            continue
        print(
            f"{describe_game(game)}: persisted {row['opinion_id']} "
            f"{row['review_status']} ({plan}; input {digest[:12]})"
        )
        summary["persisted"].append(
            {"event_id": event_id, "opinion_id": str(row["opinion_id"])}
        )
    if validation:
        # Valid rows of this week that are still pending predate the policy
        # (or came from a single-game run); the same validation approves
        # them, through the store's hash-checked review.
        earlier = week_rows(
            opinion_rows, expert_id=RATING_EXPERT_ID, week=week, season=season
        )
        if earlier and dry_run:
            print(
                f"dry run: would approve {len(earlier)} earlier valid pending "
                "row(s) on validation: "
                + ", ".join(str(row.get("opinion_id")) for row in earlier)
            )
            summary["approved_earlier"] = [
                {"opinion_id": str(row.get("opinion_id")), "dry_run": True}
                for row in earlier
            ]
        elif earlier:
            approved = approve_rows(
                store,
                earlier,
                reviewed_by=VALIDATION_REVIEWER,
                note=VALIDATION_REVIEW_NOTE,
            )
            print(
                f"approved {len(approved)} earlier valid pending row(s) on "
                "validation: " + ", ".join(approved)
            )
            summary["approved_earlier"] = [
                {
                    "opinion_id": opinion_id,
                    "event_id": str(row.get("event_id") or ""),
                }
                for opinion_id, row in zip(approved, earlier)
            ]
    print(
        f"rating week {season} wk{week}: {len(summary['persisted'])} persisted"
        f"{' (approved on validation)' if validation else ''}, "
        f"{len(summary['up_to_date'])} up to date, "
        f"{len(summary['approved_earlier'])} earlier rows approved, "
        f"{len(summary['failed'])} failed"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build every input and print the plan; persist nothing.",
    )
    args = parser.parse_args(argv)
    credentials = os.environ.get("GOOGLE_CREDENTIALS", "")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID", "")
    if not credentials or not sheet_id:
        raise RuntimeError("GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required")
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    games = spreadsheet.worksheet("nfl_games").get_all_records(
        expected_headers=GAME_HEADERS
    )
    history = spreadsheet.worksheet(GAME_HISTORY_TAB).get_all_records(
        expected_headers=GAME_HISTORY_HEADERS
    )
    store = configured_opinion_store()
    finals = current_season_finals(history, args.season)
    asyncio.run(
        generate_week(
            games=games,
            opinion_rows=store.list(),
            finals=finals,
            store=store,
            season=args.season,
            week=args.week,
            history=history,
            dry_run=args.dry_run,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
