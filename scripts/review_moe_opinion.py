#!/usr/bin/env python3
"""Approve or reject an exact persisted MOE opinion."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from moe import configured_opinion_store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opinion-id", required=True)
    parser.add_argument(
        "--status",
        required=True,
        choices=("approved", "rejected"),
    )
    parser.add_argument("--reviewed-by", required=True)
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    configured_opinion_store().review(
        args.opinion_id,
        status=args.status,
        reviewed_by=args.reviewed_by,
        note=args.note,
    )
    print(f"{args.status.title()} opinion {args.opinion_id}.")


if __name__ == "__main__":
    main()
