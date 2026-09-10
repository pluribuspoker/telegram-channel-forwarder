#!/usr/bin/env python3
"""Verify the SQLite MOE store against the archived Google Sheets source."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from moe import (
    GoogleSheetsMoeOpinionStore,
    SQLiteMoeOpinionStore,
    _sqlite_opinion_row,
    approved_opinions,
)
from scripts.migrate_moe_to_sqlite import rows_sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    sheet_rows = GoogleSheetsMoeOpinionStore(
        os.environ["GOOGLE_CREDENTIALS"],
        os.environ["NFL_INTAKE_SHEET_ID"],
        writable=False,
    ).list()
    expected = [_sqlite_opinion_row(row) for row in sheet_rows]
    store = SQLiteMoeOpinionStore(args.database.resolve(), writable=False)
    actual = store.list()
    if actual != expected:
        raise RuntimeError("SQLite rows differ from Google Sheets")
    if approved_opinions(actual) != approved_opinions(expected):
        raise RuntimeError("Approved opinion visibility differs")
    print(
        f"Verified {len(actual)} MOE rows, "
        f"{len(approved_opinions(actual))} approved, "
        f"sha256={rows_sha256(actual)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
