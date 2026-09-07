#!/usr/bin/env python3
"""Grade every approved MOE opinion against ESPN finals and closing lines.

Prints the per-expert scoreboard (Brier, ATS and O/U at close, leg record,
closing-line value), then the God Expert disagreement report: per game
graded for both arms, each arm's Brier, whether the legs agreed, and who was
right where they differed; season totals give the paired Brier difference
with its standard error, the leg agreement rate, and the disagreement
record. With --write, appends one row per graded opinion to the append-only
``moe_grades`` tab, then one ``mean_of_arms`` row per paired game (the
bake-off's free third row: a ledger row, never an expert), skipping opinion
ids already present.
"""

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
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from moe import approved_opinions, configured_opinion_store
from moe_god import (
    GRADE_HEADERS,
    GRADES_TAB,
    MEAN_OF_ARMS_ID,
    aggregator_policy,
    arm_pairs,
    build_scoreboard,
    disagreement_report,
    format_disagreement_report,
    grade_all,
    ledger_row,
    load_registry,
    mean_of_arms_results,
)
from nfl_game_history import (
    GAME_HISTORY_HEADERS,
    GAME_HISTORY_TAB,
    build_game_history,
    fetch_regular_season_events,
)
from nfl_lines import SNAPSHOT_HEADERS, _call_with_retry, get_gspread_client
from nfl_win_predictions import ensure_worksheet
from scripts.generate_moe_opinion import _latest_alignment


def _record_line(expert_id: str, record: dict) -> str:
    ats = record["ats"]
    ou = record["ou"]
    legs = record["legs"]
    brier = "—" if record["brier"] is None else f"{record['brier']:.4f}"
    clv = (
        "—"
        if record["clv_points_mean"] is None
        else f"{record['clv_points_mean']:+.2f} over {record['clv_legs']}"
    )
    return (
        f"{expert_id:<12} n={record['resolved']:<3} brier={brier:<7} "
        f"ats={ats['w']}-{ats['l']}-{ats['p']:<3} "
        f"ou={ou['w']}-{ou['l']}-{ou['p']:<3} "
        f"legs={legs['w']}-{legs['l']}-{legs['p']:<3} clv={clv}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="Season to grade (defaults to the latest season in nfl_games).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Append graded opinions to the moe_grades tab.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print the scoreboard, the disagreement report, and the "
            "mean-of-arms grades as JSON instead of text."
        ),
    )
    args = parser.parse_args()

    credentials = os.environ.get("GOOGLE_CREDENTIALS", "")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID", "")
    if not credentials or not sheet_id:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required"
        )
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    history = spreadsheet.worksheet(GAME_HISTORY_TAB).get_all_records(
        expected_headers=GAME_HISTORY_HEADERS
    )
    rows = configured_opinion_store().list()
    approved = approved_opinions(rows)
    seasons = sorted(
        {int(row["season"]) for row in approved if str(row.get("season") or "").strip()}
    )
    season = args.season or (seasons[-1] if seasons else datetime.now().year)
    events = fetch_regular_season_events(season, expected_games=None)
    finals = build_game_history(
        {season: events},
        {season: _latest_alignment(history)},
        validate=False,
        require_complete_divisional_pairs=False,
    )
    snapshots = spreadsheet.worksheet("nfl_line_snapshots").get_all_records(
        expected_headers=SNAPSHOT_HEADERS
    )
    registry = load_registry()
    policy = aggregator_policy(registry)
    graded_at = datetime.now(timezone.utc).isoformat()
    scoreboard = build_scoreboard(
        approved,
        finals=finals,
        snapshots=snapshots,
        registry=registry,
        policy=policy,
        as_of=graded_at,
    )
    graded = grade_all(
        approved,
        finals=finals,
        snapshots=snapshots,
        registry=registry,
        policy=policy,
    )
    pairs = arm_pairs(approved, graded)
    report = disagreement_report(pairs)
    means = mean_of_arms_results(pairs, finals=finals, snapshots=snapshots)
    if args.json:
        print(
            json.dumps(
                {
                    "scoreboard": scoreboard,
                    "disagreement": report,
                    "mean_of_arms": means,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            f"Season {season}: {scoreboard['resolved_games']} resolved games, "
            f"{scoreboard['graded_opinions']} graded opinions "
            f"({len(approved)} approved rows, {len(finals)} finals)."
        )
        for expert_id, record in sorted(scoreboard["by_expert"].items()):
            print(_record_line(expert_id, record))
        print()
        for line in format_disagreement_report(report):
            print(line)
    if not args.write:
        return
    worksheet = ensure_worksheet(spreadsheet, GRADES_TAB, GRADE_HEADERS)
    existing = set(
        _call_with_retry(
            worksheet.col_values, GRADE_HEADERS.index("opinion_id") + 1
        )[1:]
    )
    new_rows = [
        ledger_row(result, graded_at_utc=graded_at)
        for result in graded + means
        if result["opinion_id"] not in existing
    ]
    if not new_rows:
        print("moe_grades is already up to date.")
        return
    _call_with_retry(
        worksheet.append_rows,
        [[row.get(header, "") for header in GRADE_HEADERS] for row in new_rows],
        value_input_option="RAW",
    )
    mean_count = sum(
        1 for row in new_rows if row["expert_id"] == MEAN_OF_ARMS_ID
    )
    print(
        f"Appended {len(new_rows)} graded rows to {GRADES_TAB} "
        f"({mean_count} mean-of-arms)."
    )


if __name__ == "__main__":
    main()
