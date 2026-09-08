#!/usr/bin/env python3
"""Approve or reject persisted MOE opinions.

Two modes, both a human's command; nothing here approves on its own.

Single row (unchanged)::

    python scripts/review_moe_opinion.py --opinion-id <id> \\
        --status approved --reviewed-by <you> [--note ...]

Bulk review of one expert's week (the rating voice's normal path)::

    python scripts/review_moe_opinion.py --expert rating_elo --week 3 \\
        [--season 2026] --reviewed-by <you>            # list only
    python scripts/review_moe_opinion.py --expert rating_elo --week 3 \\
        [--season 2026] --reviewed-by <you> --approve  # approve the list

The bulk mode prints the week's valid, pending rows of that expert as one
table -- game, kickoff ET, winner, score, p(home), margin, stars, opinion id,
input hash -- and approves each of them through the store's hash-checked
``review`` only when ``--approve`` is given. Look at the table first.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from moe import MoeOpinionStore, configured_opinion_store

ET = ZoneInfo("America/New_York")
TABLE_COLUMNS = (
    "game",
    "kickoff_et",
    "winner",
    "score",
    "p_home",
    "margin",
    "stars",
    "opinion_id",
    "input",
)


def _parse_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _kickoff_et(row: dict[str, Any]) -> str:
    raw = row.get("commence_time_utc") or row.get("commence_time_et")
    if not raw:
        return ""
    kickoff = _parse_time(raw).astimezone(ET)
    clock = kickoff.strftime("%I:%M %p").lstrip("0")
    return f"{kickoff:%a %b} {kickoff.day} {clock}"


def week_rows(
    rows: Iterable[dict[str, Any]],
    *,
    expert_id: str,
    week: int,
    season: int | None = None,
    review_status: str = "pending",
) -> list[dict[str, Any]]:
    """Valid rows of one expert for one week, in kickoff order.

    ``review_status`` filters (``pending`` by default, the only rows a bulk
    approval may touch); ``season`` narrows a sheet that spans seasons.
    """
    selected = []
    for row in rows:
        if str(row.get("expert_id") or "") != expert_id:
            continue
        if str(row.get("generation_status") or "") != "valid":
            continue
        if review_status and str(row.get("review_status") or "") != review_status:
            continue
        if str(row.get("week") or "").strip() == "" or int(row["week"]) != int(week):
            continue
        if season is not None and (
            str(row.get("season") or "").strip() == "" or int(row["season"]) != int(season)
        ):
            continue
        selected.append(row)
    selected.sort(
        key=lambda row: (
            str(row.get("commence_time_utc") or ""),
            str(row.get("home_team") or ""),
            str(row.get("generated_at_utc") or ""),
            str(row.get("opinion_id") or ""),
        )
    )
    return selected


def table_cells(row: dict[str, Any]) -> dict[str, str]:
    return {
        "game": f"{row.get('away_team')} @ {row.get('home_team')}",
        "kickoff_et": _kickoff_et(row),
        "winner": str(row.get("predicted_winner") or ""),
        "score": f"{row.get('predicted_away_score')}-{row.get('predicted_home_score')}",
        "p_home": format(float(row.get("home_win_probability") or 0), ".3f"),
        "margin": format(float(row.get("expected_home_margin") or 0), "+.1f"),
        "stars": "★" * int(row.get("confidence_stars") or 0),
        "opinion_id": str(row.get("opinion_id") or ""),
        "input": str(row.get("input_sha256") or "")[:12],
    }


def format_week_table(rows: list[dict[str, Any]]) -> list[str]:
    """Aligned text lines: a header, one line per row, no row = one line."""
    if not rows:
        return ["no rows"]
    cells = [table_cells(row) for row in rows]
    widths = {
        column: max(len(column), *(len(item[column]) for item in cells))
        for column in TABLE_COLUMNS
    }
    lines = ["  ".join(column.ljust(widths[column]) for column in TABLE_COLUMNS)]
    for item in cells:
        lines.append(
            "  ".join(item[column].ljust(widths[column]) for column in TABLE_COLUMNS).rstrip()
        )
    return lines


def approve_rows(
    store: MoeOpinionStore,
    rows: Iterable[dict[str, Any]],
    *,
    reviewed_by: str,
    note: str = "",
) -> list[str]:
    """Approve every row through the store's hash-checked review; returns
    the opinion ids in the order they were approved."""
    approved = []
    for row in rows:
        opinion_id = str(row["opinion_id"])
        store.review(
            opinion_id, status="approved", reviewed_by=reviewed_by, note=note
        )
        approved.append(opinion_id)
    return approved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--opinion-id", help="single-row mode: the opinion to review")
    parser.add_argument(
        "--status",
        choices=("approved", "rejected"),
        help="single-row mode: the verdict",
    )
    parser.add_argument(
        "--expert",
        help="bulk mode: list (and with --approve, approve) one expert's pending rows for a week",
    )
    parser.add_argument("--week", type=int, help="bulk mode: the NFL week")
    parser.add_argument("--season", type=int, help="bulk mode: the season (default: every season)")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="bulk mode: approve every listed row (without it the table is only printed)",
    )
    parser.add_argument("--reviewed-by", required=True)
    parser.add_argument("--note", default="")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.expert:
        if args.week is None:
            parser.error("--expert requires --week")
        if args.opinion_id or args.status:
            parser.error("--expert (bulk mode) does not take --opinion-id or --status")
    else:
        if not args.opinion_id or not args.status:
            parser.error("--opinion-id and --status are required (or use --expert --week)")
        if args.approve or args.week is not None or args.season is not None:
            parser.error("--approve, --week and --season belong to bulk mode (--expert)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = configured_opinion_store()
    if not args.expert:
        store.review(
            args.opinion_id,
            status=args.status,
            reviewed_by=args.reviewed_by,
            note=args.note,
        )
        print(f"{args.status.title()} opinion {args.opinion_id}.")
        return 0
    rows = week_rows(
        store.list(), expert_id=args.expert, week=args.week, season=args.season
    )
    label = f"{args.expert} week {args.week}" + (
        f" season {args.season}" if args.season is not None else ""
    )
    print(f"{len(rows)} valid pending row(s) for {label}")
    for line in format_week_table(rows):
        print(line)
    if not rows:
        return 0
    if not args.approve:
        print(f"Listing only; re-run with --approve to approve these {len(rows)} row(s).")
        return 0
    approved = approve_rows(store, rows, reviewed_by=args.reviewed_by, note=args.note)
    print(f"Approved {len(approved)} row(s) for {label} as {args.reviewed_by}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
