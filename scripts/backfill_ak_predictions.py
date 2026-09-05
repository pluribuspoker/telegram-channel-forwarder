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
    args = parser.parse_args()

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
        report.append(
            {
                "row_number": row_number,
                "submission_id": row.get("submission_id", ""),
                "event_id": row.get("event_id", ""),
                "away_team": row.get("away_team", ""),
                "home_team": row.get("home_team", ""),
                "lean_text": row.get("lean_text", ""),
                **parsed,
            }
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
            "1",
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
