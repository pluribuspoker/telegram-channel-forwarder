#!/usr/bin/env python3
"""Create and verify an online backup of the authoritative MOE SQLite store."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from moe import SQLiteMoeOpinionStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=os.getenv("MOE_SQLITE_PATH"),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--retain", type=int, default=14)
    args = parser.parse_args()
    if args.database is None:
        parser.error("--database or MOE_SQLITE_PATH is required")
    if args.retain < 1:
        parser.error("--retain must be at least 1")
    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    SQLiteMoeOpinionStore(database, writable=False)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else database.parent / "backups"
    )
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = output_dir / f"moe-{timestamp}.sqlite3"
    temporary = output_dir / f".moe-{timestamp}.sqlite3.tmp"
    if output.exists():
        raise FileExistsError(output)
    if temporary.exists():
        temporary.unlink()
    try:
        with sqlite3.connect(database, timeout=30) as source:
            with sqlite3.connect(temporary) as target:
                source.backup(target)
        os.chmod(temporary, 0o600)
        with sqlite3.connect(temporary) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup quick_check failed")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity_check failed")
        SQLiteMoeOpinionStore(temporary, writable=False)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    backups = sorted(output_dir.glob("moe-*.sqlite3"), reverse=True)
    for expired in backups[args.retain :]:
        expired.unlink()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
