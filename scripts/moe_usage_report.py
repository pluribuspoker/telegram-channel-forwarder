#!/usr/bin/env python3
"""MOE token/usage look-back report.

Aggregates the two places MOE usage is recorded:

- ``logs/god_judge_runs.jsonl`` — every headless claude call the judge
  runner makes (judge calls and late-window voice refreshes), with exact
  token usage and notional cost from the CLI's result envelope.
- The ``moe_opinions`` store — every persisted opinion row, whatever path
  generated it. Agent sessions bill their own runtime and expose no token
  counts at persist time, so those rows report response size (chars) as the
  proxy; ``--api`` dollars additionally land in ``logs/claude_spend.jsonl``
  via the ``ai.py`` choke point.

Read-only. Run on the VPS (the opinion store lives there):

    ~/venv/bin/python scripts/moe_usage_report.py --days 7
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

DEFAULT_RUNS_LOG = ROOT / "logs" / "god_judge_runs.jsonl"


def _parse_time(value: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def summarize_runs(
    runs: Iterable[dict[str, Any]],
    *,
    since: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Per call kind (judge calls, voice refreshes per expert): counts,
    exact token totals, notional cost, and per-day call counts."""
    buckets: dict[str, dict[str, Any]] = {}
    for record in runs:
        stamped = _parse_time(str(record.get("logged_at_utc") or ""))
        if since is not None and (stamped is None or stamped < since):
            continue
        kind = str(record.get("kind") or "unknown")
        if kind == "voice_refresh_call":
            kind = f"refresh:{record.get('expert_id') or '?'}"
        elif kind == "claude_call":
            kind = "judge"
        bucket = buckets.setdefault(
            kind,
            {
                "calls": 0,
                "errors": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "cost_usd": 0.0,
                "by_day": defaultdict(int),
            },
        )
        bucket["calls"] += 1
        if str(record.get("status") or "") != "ok":
            bucket["errors"] += 1
        usage = record.get("usage") or {}
        bucket["input_tokens"] += int(usage.get("input_tokens") or 0)
        bucket["output_tokens"] += int(usage.get("output_tokens") or 0)
        bucket["cache_read_tokens"] += int(
            usage.get("cache_read_input_tokens") or 0
        )
        bucket["cache_creation_tokens"] += int(
            usage.get("cache_creation_input_tokens") or 0
        )
        bucket["cost_usd"] += float(record.get("total_cost_usd") or 0)
        if stamped is not None:
            bucket["by_day"][stamped.date().isoformat()] += 1
    for bucket in buckets.values():
        bucket["by_day"] = dict(sorted(bucket["by_day"].items()))
    return buckets


def summarize_opinions(
    rows: Iterable[dict[str, Any]],
    *,
    since: datetime | None = None,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Per (expert, backend, model): row counts by generation status and
    response size in characters — the honest proxy where exact tokens are
    not recorded (agent-session generations)."""
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        stamped = _parse_time(str(row.get("generated_at_utc") or ""))
        if since is not None and (stamped is None or stamped < since):
            continue
        key = (
            str(row.get("expert_id") or "?"),
            str(row.get("generation_backend") or "?"),
            str(row.get("model") or "?"),
        )
        bucket = buckets.setdefault(
            key,
            {"rows": 0, "by_status": defaultdict(int), "response_chars": 0},
        )
        bucket["rows"] += 1
        bucket["by_status"][str(row.get("generation_status") or "?")] += 1
        bucket["response_chars"] += len(str(row.get("raw_response") or ""))
    for bucket in buckets.values():
        bucket["by_status"] = dict(sorted(bucket["by_status"].items()))
    return buckets


def _load_runs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=float,
        default=7,
        help="Look-back window in days (default 7; 0 = everything).",
    )
    parser.add_argument(
        "--runs-log", type=Path, default=DEFAULT_RUNS_LOG,
        help="The judge runner's usage ledger.",
    )
    parser.add_argument(
        "--skip-opinions",
        action="store_true",
        help="Only read the runs ledger (no opinion store access).",
    )
    args = parser.parse_args(argv)
    since = (
        datetime.now(timezone.utc) - timedelta(days=args.days)
        if args.days
        else None
    )
    window = f"last {args.days:g} days" if since else "all time"

    print(f"=== headless claude calls ({window}; exact tokens) ===")
    run_summary = summarize_runs(_load_runs(args.runs_log), since=since)
    if not run_summary:
        print("no calls in window")
    for kind in sorted(run_summary):
        b = run_summary[kind]
        print(
            f"{kind}: {b['calls']} calls ({b['errors']} errors), "
            f"in={b['input_tokens']} out={b['output_tokens']} "
            f"cache_read={b['cache_read_tokens']} "
            f"cache_create={b['cache_creation_tokens']}, "
            f"${b['cost_usd']:.2f} notional"
        )
        for day, count in b["by_day"].items():
            print(f"    {day}: {count}")

    if args.skip_opinions:
        return 0
    from moe import configured_opinion_store

    print(f"\n=== persisted opinion rows ({window}; size proxy) ===")
    opinion_summary = summarize_opinions(
        configured_opinion_store().list(), since=since
    )
    if not opinion_summary:
        print("no rows in window")
    for key in sorted(opinion_summary):
        expert, backend, model = key
        b = opinion_summary[key]
        statuses = ", ".join(
            f"{status}={count}" for status, count in b["by_status"].items()
        )
        mean = b["response_chars"] // max(b["rows"], 1)
        print(
            f"{expert} via {backend} on {model}: {b['rows']} rows "
            f"({statuses}), {b['response_chars']} response chars "
            f"(mean {mean})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
