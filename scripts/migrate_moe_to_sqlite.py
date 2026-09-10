#!/usr/bin/env python3
"""Copy the complete MOE opinion store from Google Sheets to SQLite."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from moe import (
    OPINION_HEADERS,
    GoogleSheetsMoeOpinionStore,
    SQLiteMoeOpinionStore,
    _artifact_refs,
    _canonical_json,
    _sqlite_opinion_row,
    approved_opinions,
)


def rows_sha256(rows: list[dict[str, str]]) -> str:
    values = [[row[header] for header in OPINION_HEADERS] for row in rows]
    return hashlib.sha256(_canonical_json(values).encode("utf-8")).hexdigest()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New SQLite path; the command refuses to overwrite it.",
    )
    result.add_argument(
        "--backup-json",
        type=Path,
        required=True,
        help="Mode-0600 lossless JSON export written before the SQLite import.",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    output = args.output.expanduser().resolve()
    backup = args.backup_json.expanduser().resolve()
    if output.exists():
        raise FileExistsError(output)
    if backup.exists():
        raise FileExistsError(backup)
    source = GoogleSheetsMoeOpinionStore(
        os.environ["GOOGLE_CREDENTIALS"],
        os.environ["NFL_INTAKE_SHEET_ID"],
        writable=False,
    )
    source._opinion_worksheet(create=False)
    sheet_rows = source.list()
    if not sheet_rows:
        raise RuntimeError("Refusing to migrate an empty MOE opinion worksheet")
    sqlite_rows = [_sqlite_opinion_row(row) for row in sheet_rows]
    chunked_fields = sum(len(_artifact_refs(row)) for row in sheet_rows)
    export = {
        "schema": OPINION_HEADERS,
        "row_count": len(sheet_rows),
        "sqlite_rows_sha256": rows_sha256(sqlite_rows),
        "chunked_fields_reconstructed": chunked_fields,
        "rows": sheet_rows,
    }
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup.write_text(
        json.dumps(export, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(backup, 0o600)

    target = SQLiteMoeOpinionStore(output, create=True)
    target.append_rows(sheet_rows, journal=False)
    imported = target.list()
    if imported != sqlite_rows:
        raise RuntimeError("SQLite rows differ from the reconstructed Sheet rows")
    sheet_approved = {
        (str(row["opinion_id"]), str(row["approved_output_sha256"]))
        for row in approved_opinions(sheet_rows)
    }
    sqlite_approved = {
        (str(row["opinion_id"]), str(row["approved_output_sha256"]))
        for row in approved_opinions(imported)
    }
    if sqlite_approved != sheet_approved:
        raise RuntimeError("Approved MOE hashes changed during migration")
    final_sheet_rows = source.list()
    if [_sqlite_opinion_row(row) for row in final_sheet_rows] != sqlite_rows:
        raise RuntimeError("MOE Sheet changed during migration; cutover aborted")
    with target._connect() as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity_check failed")
    print(
        json.dumps(
            {
                "output": str(output),
                "backup_json": str(backup),
                "rows": len(imported),
                "approved": len(sqlite_approved),
                "chunked_fields_reconstructed": chunked_fields,
                "sha256": rows_sha256(imported),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
