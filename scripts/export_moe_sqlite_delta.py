#!/usr/bin/env python3
"""Export post-migration MOE SQLite writes for verified Sheet rollback."""

from __future__ import annotations

import argparse
import json
import os
import sys
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
    parser.add_argument("--after-sequence", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.database is None:
        parser.error("--database or MOE_SQLITE_PATH is required")
    output = args.output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(output)
    store = SQLiteMoeOpinionStore(
        args.database.expanduser().resolve(),
        writable=False,
    )
    entries = store.journal_entries(args.after_sequence)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.write_text(
        json.dumps(
            {
                "after_sequence": args.after_sequence,
                "entries": entries,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(output, 0o600)
    print(f"Exported {len(entries)} MOE write(s) to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
