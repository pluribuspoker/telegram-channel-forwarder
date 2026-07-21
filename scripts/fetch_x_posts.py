"""Fetch posts from an X/Twitter user and write to CSV.

Usage:
    X_AUTH_TOKEN=xxx X_CT0=yyy python scripts/fetch_x_posts.py
    X_AUTH_TOKEN=xxx X_CT0=yyy python scripts/fetch_x_posts.py --username SomeUser --since 2025-07-01
    X_AUTH_TOKEN=xxx X_CT0=yyy python scripts/fetch_x_posts.py --output scripts/output/custom.csv
"""

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from twscrape import gather

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.x_client import XCredentialsError, build_api


async def fetch_tweets(username: str, since: datetime, limit: int = 2000):
    try:
        api = await build_api()
    except XCredentialsError as e:
        print(e)
        print("DevTools (F12) → Application → Cookies → https://x.com")
        return []

    user = await api.user_by_login(username)
    if user is None:
        print(f"X rejected the cookies — could not resolve @{username}. Refresh them.")
        return []
    print(f"Fetching tweets for @{user.username} (id={user.id}) since {since.date()}")

    results = []
    async for tw in api.user_tweets(user.id, limit=limit):
        if tw.date < since:
            break
        photos = [m.url for m in tw.media.photos] if tw.media else []
        videos = [m.thumbnailUrl for m in tw.media.videos] if tw.media else []
        results.append({
            "id": tw.id,
            "date": tw.date.isoformat(),
            "text": tw.rawContent,
            "photos": "|".join(photos),
            "videos": "|".join(videos),
            "url": f"https://x.com/{user.username}/status/{tw.id}",
        })

    print(f"Fetched {len(results)} tweets")
    return results


def write_csv(rows: list[dict], path: str):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {path}")


def main():
    parser = argparse.ArgumentParser(description="Fetch X/Twitter user posts to CSV")
    parser.add_argument("--username", default="BookitWithTrent")
    parser.add_argument("--since", default="2025-06-12", help="YYYY-MM-DD cutoff date")
    parser.add_argument("--output", default=None, help="Output CSV path")
    parser.add_argument("--limit", type=int, default=2000, help="Max tweets to scan")
    args = parser.parse_args()

    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    output_dir = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(output_dir, exist_ok=True)
    output = args.output or os.path.join(output_dir, f"{args.username}_posts.csv")

    rows = asyncio.run(fetch_tweets(args.username, since, args.limit))
    if rows:
        write_csv(rows, output)


if __name__ == "__main__":
    main()
