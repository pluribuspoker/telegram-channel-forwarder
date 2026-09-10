#!/usr/bin/env python3
"""Backtest the God Expert rules arm on historical lines (roadmap WP8) and
refit it on the live ledger (WP10).

Four modes, all deterministic, standard library only::

    python scripts/backtest_god.py grid   [--fit-seasons 2023-2024] [--check-seasons 2025] [--json out.json]
    python scripts/backtest_god.py veto   [--seasons 2024-2025] [--json out.json]
    python scripts/backtest_god.py clv    [--seasons 2024-2025] [--grid-json grid.json] [--json out.json]
    python scripts/backtest_god.py ledger [--rows-json rows.json --finals-json finals.json [--snapshots-json snaps.json]]
                                          [--include-pending] [--grid-json grid.json] [--season 2026] [--json out.json]

``grid`` fits the policy grid on the fit seasons (the rating voice alone as
the committee, the nflverse close as the market), prints the selection with
its rule, and confirms the chosen policy beside the registry policy on the
check seasons, which the selection never saw. It prints the resulting
``aggregator_policy`` block but never writes ``moe/experts.yaml``.

``veto`` tabulates, per threshold, how the side that got cheaper since the
ESPN open did at the ESPN close; ``clv`` places the arm's bets at the ESPN
open and grades and CLV's them against the ESPN close (``--grid-json``
reuses a ``grid`` run's chosen policy; without it the grid is refitted
first); ``ledger`` replays persisted ``god_rules`` rows under the same grid
and veto sweep, from JSON files or, on the VPS, from the sheet and ESPN
finals (reads only; nothing is persisted or approved). Every number comes
from ``moe_backtest``, which runs the production arithmetic.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import moe_backtest as bt  # noqa: E402


def _write_json(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bt.json_text(payload), encoding="utf-8", newline="\n")
    print(f"wrote {path}")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _selection_from_grid_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    document = _load_json(path)
    selection = document.get("selection") if isinstance(document, dict) else None
    if not isinstance(selection, dict) or "overrides" not in selection:
        raise SystemExit(f"{path} is not a grid run (no selection block)")
    return selection


def _sheet_ledger_inputs(season: int | None) -> tuple[list[dict], list[dict], list[dict]]:
    """Rows, finals and snapshots from the sheet and ESPN (VPS only)."""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env.local")
    load_dotenv(ROOT / ".env")
    from moe import configured_opinion_store
    from nfl_game_annotations import (
        attach_game_annotations,
        load_game_annotations,
    )
    from nfl_game_history import GAME_HISTORY_HEADERS, GAME_HISTORY_TAB
    from nfl_lines import SNAPSHOT_HEADERS, get_gspread_client
    from scripts.generate_moe_opinion import current_season_finals

    credentials = os.environ.get("GOOGLE_CREDENTIALS", "")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID", "")
    if not credentials or not sheet_id:
        raise SystemExit(
            "GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required (or pass "
            "--rows-json/--finals-json)"
        )
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    history = spreadsheet.worksheet(GAME_HISTORY_TAB).get_all_records(
        expected_headers=GAME_HISTORY_HEADERS
    )
    annotation_rows = load_game_annotations(spreadsheet)
    history = attach_game_annotations(history, annotation_rows)
    rows = configured_opinion_store().list()
    seasons = sorted(
        {
            int(row["season"])
            for row in rows
            if str(row.get("expert_id") or "") == bt.RULES_EXPERT_ID
            and str(row.get("season") or "").strip()
        }
    )
    if season is None:
        if not seasons:
            raise SystemExit("No god_rules rows in the sheet")
        season = seasons[-1]
    finals = current_season_finals(history, season, annotation_rows)
    snapshots = spreadsheet.worksheet("nfl_line_snapshots").get_all_records(
        expected_headers=SNAPSHOT_HEADERS
    )
    return rows, finals, snapshots


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--csv", type=Path, default=bt.LINES_CSV, help="nflverse lines CSV")
    parser.add_argument("--open-close", type=Path, default=bt.OPEN_CLOSE_JSON, help="ESPN open/close JSON")
    parser.add_argument("--json", type=Path, default=None, help="write every result to this file")
    modes = parser.add_subparsers(dest="mode")
    grid = modes.add_parser("grid", help="fit the policy grid and confirm on held-out seasons")
    grid.add_argument("--fit-seasons", nargs="+", default=[f"{bt.DEFAULT_FIT_SEASONS[0]}-{bt.DEFAULT_FIT_SEASONS[-1]}"])
    grid.add_argument("--check-seasons", nargs="+", default=[str(season) for season in bt.DEFAULT_CHECK_SEASONS])
    veto = modes.add_parser("veto", help="calibrate the veto thresholds on ESPN open -> close")
    veto.add_argument("--seasons", nargs="+", default=[f"{bt.DEFAULT_CLV_SEASONS[0]}-{bt.DEFAULT_CLV_SEASONS[-1]}"])
    clv = modes.add_parser("clv", help="bets at the ESPN open, graded and CLV'd at the ESPN close")
    clv.add_argument("--seasons", nargs="+", default=[f"{bt.DEFAULT_CLV_SEASONS[0]}-{bt.DEFAULT_CLV_SEASONS[-1]}"])
    clv.add_argument("--grid-json", type=Path, default=None, help="a grid run's JSON; its chosen policy is replayed")
    clv.add_argument("--fit-seasons", nargs="+", default=[f"{bt.DEFAULT_FIT_SEASONS[0]}-{bt.DEFAULT_FIT_SEASONS[-1]}"], help="fit seasons when the grid is refitted here")
    ledger = modes.add_parser("ledger", help="refit on persisted god_rules rows (WP10)")
    ledger.add_argument("--rows-json", type=Path, default=None, help="opinion rows (a JSON list); default: the sheet")
    ledger.add_argument("--finals-json", type=Path, default=None, help="finals shaped like nfl_game_history rows")
    ledger.add_argument("--snapshots-json", type=Path, default=None, help="nfl_line_snapshots rows (optional)")
    ledger.add_argument("--season", type=int, default=None, help="season to grade from the sheet (default: latest with rows)")
    ledger.add_argument("--include-pending", action="store_true", help="replay pending rows too, not only approved ones")
    ledger.add_argument("--grid-json", type=Path, default=None, help="a grid run's JSON to compare the ledger's selection against")
    return parser


def main(argv: list[str] | None = None) -> int:
    # The reports carry star glyphs; a cp1252 console (Windows) must not
    # crash the run after the arithmetic is done.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = args.mode or "grid"
    rows = bt.load_rows(args.csv)
    if mode == "grid":
        result = bt.run_grid(
            rows,
            fit_seasons=bt.parse_seasons(args.fit_seasons),
            check_seasons=bt.parse_seasons(args.check_seasons),
        )
        print("\n".join(bt.format_grid_report(result)))
        _write_json(args.json, result)
        return 0
    open_close = bt.load_open_close(args.open_close)
    if mode == "veto":
        result = bt.run_veto(rows, seasons=bt.parse_seasons(args.seasons), open_close=open_close)
        print("\n".join(bt.format_veto_report(result)))
        _write_json(args.json, result)
        return 0
    if mode == "clv":
        selection = _selection_from_grid_json(args.grid_json)
        if selection is None:
            fitted = bt.run_grid(rows, fit_seasons=bt.parse_seasons(args.fit_seasons), check_seasons=bt.parse_seasons(args.fit_seasons))
            selection = fitted["selection"]
            print(f"grid refitted on {fitted['fit_seasons']}: chosen {selection['key']}")
        result = bt.run_clv(rows, seasons=bt.parse_seasons(args.seasons), selection=selection, open_close=open_close)
        print("\n".join(bt.format_clv_report(result)))
        _write_json(args.json, result)
        return 0
    if mode == "ledger":
        if (args.rows_json is None) != (args.finals_json is None):
            parser.error("--rows-json and --finals-json go together")
        if args.rows_json is not None:
            opinion_rows = _load_json(args.rows_json)
            finals = _load_json(args.finals_json)
            snapshots = _load_json(args.snapshots_json) if args.snapshots_json else []
        else:
            opinion_rows, finals, snapshots = _sheet_ledger_inputs(args.season)
        fitted = _selection_from_grid_json(args.grid_json)
        result = bt.run_ledger(
            opinion_rows,
            finals=finals,
            snapshots=snapshots,
            approved_only=not args.include_pending,
            fitted=fitted,
        )
        print("\n".join(bt.format_ledger_report(result)))
        _write_json(args.json, result)
        return 0
    parser.error(f"unknown mode {mode}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
