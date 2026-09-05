#!/usr/bin/env python3
"""Report, migrate, or apply deterministic AK projected-score normalization."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from gspread.utils import rowcol_to_a1

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from moe_ak import parse_ak_projection
from moe import OPINION_HEADERS, OPINIONS_TAB
from nfl_lines import LEAN_HEADERS, get_gspread_client


NORMALIZED_HEADERS = [
    "predicted_away_score",
    "predicted_home_score",
    "prediction_parse_version",
    "prediction_parse_status",
]
AK_OPINION_HEADERS = [
    "side_pick_json",
    "total_pick_json",
    "calibration_summary_json",
]


def apply_reviewed_override(
    item: dict,
    override: dict,
) -> dict:
    away_score = override.get("away_score")
    home_score = override.get("home_score")
    if (
        isinstance(away_score, bool)
        or not isinstance(away_score, int)
        or isinstance(home_score, bool)
        or not isinstance(home_score, int)
        or away_score < 0
        or home_score < 0
        or away_score == home_score
    ):
        raise ValueError(
            f"Invalid reviewed override for event {item['event_id']}"
        )
    winner = (
        item["away_team"] if away_score > home_score else item["home_team"]
    )
    return {
        **item,
        "away_score": away_score,
        "home_score": home_score,
        "winner": winner,
        "loser": (
            item["home_team"] if winner == item["away_team"]
            else item["away_team"]
        ),
        "status": "parsed",
        "parse_version": "human_v1",
        "reason": "Human-reviewed score ownership override",
    }


def append_headers(worksheet, expected: list[str], appended: list[str]) -> None:
    current = worksheet.row_values(1)
    old = expected[: -len(appended)]
    if current == expected:
        return
    if current != old:
        raise RuntimeError(
            f"{worksheet.title} headers do not match the guarded old schema"
        )
    worksheet.add_cols(len(appended))
    worksheet.update(
        [expected],
        f"A1:{rowcol_to_a1(1, len(expected))}",
        value_input_option="RAW",
    )
    if worksheet.row_values(1) != expected:
        raise RuntimeError(f"{worksheet.title} header migration failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--overrides",
        type=Path,
        help="Reviewed event-id to away/home score JSON overrides",
    )
    args = parser.parse_args()
    overrides = (
        json.loads(args.overrides.read_text(encoding="utf-8"))
        if args.overrides
        else {}
    )
    if not isinstance(overrides, dict):
        raise ValueError("--overrides must contain a JSON object")

    ak_user_id = os.environ.get("AK_TELEGRAM_USER_ID", "").strip()
    if not ak_user_id:
        raise RuntimeError("AK_TELEGRAM_USER_ID is required")
    spreadsheet = get_gspread_client(
        os.environ["GOOGLE_CREDENTIALS"]
    ).open_by_key(os.environ["NFL_INTAKE_SHEET_ID"])
    worksheet = spreadsheet.worksheet("nfl_leans")
    values = worksheet.get_all_values()
    headers = values[0] if values else []
    old_headers = LEAN_HEADERS[: -len(NORMALIZED_HEADERS)]
    if args.migrate:
        append_headers(worksheet, LEAN_HEADERS, NORMALIZED_HEADERS)
        append_headers(
            spreadsheet.worksheet(OPINIONS_TAB),
            OPINION_HEADERS,
            AK_OPINION_HEADERS,
        )
        values = worksheet.get_all_values()
        headers = values[0]
    if headers != old_headers and headers != LEAN_HEADERS:
        raise RuntimeError("nfl_leans headers do not match old or new schema")
    records = [
        (row_number, dict(zip(headers, row)))
        for row_number, row in enumerate(values[1:], start=2)
        if row and str(row[0]).strip()
    ]
    report = []
    for row_number, row in records:
        if str(row.get("telegram_user_id") or "") != ak_user_id:
            continue
        parsed = parse_ak_projection(
            str(row.get("lean_text") or ""),
            away_team=str(row.get("away_team") or ""),
            home_team=str(row.get("home_team") or ""),
        )
        item = {
            "row_number": row_number,
            "submission_id": row.get("submission_id", ""),
            "event_id": row.get("event_id", ""),
            "away_team": row.get("away_team", ""),
            "home_team": row.get("home_team", ""),
            "lean_text": row.get("lean_text", ""),
            "parse_version": 1 if parsed["status"] == "parsed" else "",
            **parsed,
        }
        event_id = str(item["event_id"])
        if event_id in overrides:
            item = apply_reviewed_override(item, overrides[event_id])
        report.append(item)
    unused_overrides = set(overrides) - {
        str(item["event_id"]) for item in report
    }
    if unused_overrides:
        raise ValueError(
            f"Overrides do not match an AK submission: {sorted(unused_overrides)}"
        )
    if args.report:
        args.report.write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    counts: dict[str, int] = {}
    for item in report:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    print(json.dumps({"rows": len(report), "statuses": counts}, sort_keys=True))
    if not args.apply:
        return
    if headers != LEAN_HEADERS:
        raise RuntimeError("Run with --migrate before --apply")
    start_col = len(LEAN_HEADERS) - len(NORMALIZED_HEADERS) + 1
    for item in report:
        if item["status"] != "parsed":
            continue
        row_number = int(item["row_number"])
        existing = worksheet.row_values(row_number)
        normalized_existing = existing[start_col - 1 : start_col + 3]
        desired = [
            str(item["away_score"]),
            str(item["home_score"]),
            str(item["parse_version"]),
            "parsed",
        ]
        if [str(value) for value in normalized_existing] == desired:
            continue
        if any(str(value).strip() for value in normalized_existing):
            raise RuntimeError(
                f"Refusing to overwrite normalized row {row_number}"
            )
        worksheet.update(
            [desired],
            f"{rowcol_to_a1(row_number, start_col)}:"
            f"{rowcol_to_a1(row_number, start_col + 3)}",
            value_input_option="RAW",
        )


if __name__ == "__main__":
    main()
