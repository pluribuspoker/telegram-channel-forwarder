#!/usr/bin/env python3
"""One-time backfill: add Pikkit splits to existing parse_cache entries."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()
load_dotenv(".env.local", override=True)

from pikkit import get_pick_splits

CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "parse_cache.json")


async def backfill():
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        cache = json.load(f)

    total = matched = skipped = errors = 0

    for key, entry in list(cache.items()):
        if not isinstance(entry, dict) or entry.get("_dupe"):
            continue
        parsed = entry.get("parsed")
        if not parsed or not parsed.get("picks"):
            continue
        if entry.get("pikkit_by_pick"):
            skipped += 1
            continue

        date_str = entry.get("msg_date")
        if not date_str:
            continue

        total += 1
        sport = parsed.get("sport", "")
        picks = parsed["picks"]
        pikkit_by_pick = {}

        for i, pick in enumerate(picks):
            pick_sport = pick.get("sport") or sport
            if not pick_sport:
                continue
            try:
                pdata = await get_pick_splits(pick, pick_sport, date_str)
            except Exception as e:
                errors += 1
                print(f"  ERROR {key} pick {i}: {e}")
                pdata = None

            if pdata:
                pikkit_by_pick[str(i)] = pdata

        if pikkit_by_pick:
            entry["pikkit_by_pick"] = pikkit_by_pick
            matched += 1
            capper = entry.get("capper_name", "?")[:20]
            first = pikkit_by_pick.get("0", {})
            side = first.get("side", "?")
            pct = round(first.get("public_pct", 0) * 100)
            print(f"  {key} | {capper} → {side} ({pct}%)")

    # Write back
    tmp = CACHE_PATH + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_PATH)

    print(f"\nDone: {matched} matched, {total - matched - errors} no match, {skipped} already had data, {errors} errors")


if __name__ == "__main__":
    asyncio.run(backfill())
