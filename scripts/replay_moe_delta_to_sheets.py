#!/usr/bin/env python3
"""Verify or replay a MOE SQLite write-journal delta to Google Sheets."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import gspread
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from moe import (
    OPINION_HEADERS,
    GoogleSheetsMoeOpinionStore,
    _sqlite_opinion_row,
    opinion_output_sha256,
)


def chain_start(chain: list[dict], current: dict | None) -> int:
    states = [
        (
            json.loads(chain[0]["before_json"])
            if chain[0].get("before_json")
            else None
        ),
        *[json.loads(entry["after_json"]) for entry in chain],
    ]
    matching = [
        index for index, candidate in enumerate(states) if candidate == current
    ]
    if not matching:
        raise ValueError(
            f"Sheet delta conflicts with {chain[0]['opinion_id']}"
        )
    return max(matching)


def exact_review_update(
    store: GoogleSheetsMoeOpinionStore,
    opinion_id: str,
    row: dict[str, str],
) -> None:
    worksheet = store._opinion_worksheet()
    ids = worksheet.col_values(OPINION_HEADERS.index("opinion_id") + 1)
    matches = [
        row_number
        for row_number, value in enumerate(ids, start=1)
        if value == opinion_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one opinion_id match, found {len(matches)}"
        )
    start_col = OPINION_HEADERS.index("review_status") + 1
    end_col = OPINION_HEADERS.index("approved_output_sha256") + 1
    start = gspread.utils.rowcol_to_a1(matches[0], start_col)
    end = gspread.utils.rowcol_to_a1(matches[0], end_col)
    worksheet.update(
        [
            [
                row["review_status"],
                row["reviewed_at_utc"],
                row["reviewed_by"],
                row["review_note"],
                row["output_sha256"],
                row["approved_output_sha256"],
            ]
        ],
        f"{start}:{end}",
        value_input_option="RAW",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delta", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply after verification; default is verify-only.",
    )
    args = parser.parse_args()
    payload = json.loads(args.delta.read_text(encoding="utf-8"))
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Delta entries must be a list")
    sequences = [int(entry["sequence"]) for entry in entries]
    if sequences != sorted(set(sequences)):
        raise ValueError("Delta sequences must be unique and ascending")

    store = GoogleSheetsMoeOpinionStore(
        os.environ["GOOGLE_CREDENTIALS"],
        os.environ["NFL_INTAKE_SHEET_ID"],
        writable=args.apply,
    )
    store._opinion_worksheet(create=False)
    state = {
        str(row["opinion_id"]): _sqlite_opinion_row(row)
        for row in store.list()
    }
    chains: dict[str, list[dict]] = {}
    for entry in entries:
        chains.setdefault(str(entry["opinion_id"]), []).append(entry)
    start_by_id: dict[str, int] = {}
    for opinion_id, chain in chains.items():
        start_by_id[opinion_id] = chain_start(chain, state.get(opinion_id))
    positions: dict[str, int] = {}
    applied = 0
    skipped = 0
    for entry in entries:
        operation = str(entry["operation"])
        opinion_id = str(entry["opinion_id"])
        position = positions.get(opinion_id, 0)
        positions[opinion_id] = position + 1
        if position < start_by_id[opinion_id]:
            skipped += 1
            continue
        before = (
            json.loads(entry["before_json"])
            if entry.get("before_json")
            else None
        )
        after = json.loads(entry["after_json"])
        if _sqlite_opinion_row(after) != after:
            raise ValueError(f"Delta row is not normalized: {opinion_id}")
        output_sha256 = str(after.get("output_sha256") or "")
        if output_sha256 and opinion_output_sha256(after) != output_sha256:
            raise ValueError(f"Delta output hash changed: {opinion_id}")
        if (
            not output_sha256
            and str(after.get("generation_status")) not in {"invalid", "sample"}
        ):
            raise ValueError(f"Delta output hash is missing: {opinion_id}")
        current = state.get(opinion_id)
        if operation == "append":
            if current is not None:
                if current != after:
                    raise ValueError(
                        f"Sheet append conflicts with {opinion_id}"
                    )
                skipped += 1
                continue
            if args.apply:
                store.append(after)
            state[opinion_id] = after
            applied += 1
            continue
        if operation != "review" or before is None:
            raise ValueError(f"Unsupported delta operation: {operation}")
        if current != before:
            raise ValueError(f"Sheet review conflicts with {opinion_id}")
        if args.apply:
            exact_review_update(store, opinion_id, after)
        state[opinion_id] = after
        applied += 1
    action = "Applied" if args.apply else "Verified"
    print(f"{action} {applied} change(s); {skipped} already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
