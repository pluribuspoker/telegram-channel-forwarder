"""Fetch posts from an X/Twitter user and write to CSV.

Step 1 of the capper backfill pipeline:
    fetch_x_posts -> parse_posts_csv -> grade_csv -> format_graded_csv

Usage:
    python scripts/fetch_x_posts.py --username boyerBets_ --since 2026-05-07
    python scripts/fetch_x_posts.py --output scripts/output/custom.csv

Credentials (X_AUTH_TOKEN / X_CT0) are read from .env.local.
"""

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from scripts.x_client import XCredentialsError, build_api

# Pick dates are US sports-betting dates. X returns UTC, so a pick posted at
# 8pm ET lands on the NEXT UTC day and every downstream stage — ESPN scoreboard
# lookup, Odds API closing lines, the "Game date" column — is then a day off the
# game. Measured on @boyerBets_: 16 of 144 parsed picks, 3 of them graded PODs.
POST_TZ = ZoneInfo("America/New_York")

# user_tweets is not strictly reverse-chronological (83 order inversions in one
# @boyerBets_ pull), so a single out-of-order tweet older than --since would end
# the scan early and silently truncate the corpus. Require a run of them.
STOP_AFTER_CONSECUTIVE_OLD = 25


def _source_tweet(tw, username: str):
    """Return the tweet that actually carries the content.

    X's profile timeline (the UserTweets endpoint) shows a SELF-retweet *in place
    of* the tweet it retweets — the original is never emitted at all, even though
    it is a normal top-level post that tweet_details returns fine. For
    @boyerBets_ that hid the original of 45 of 50 self-retweets, including the
    2026-07-24 "Twins F5 +0.5" Play of the Day.

    twscrape already hands us the full original as `tw.retweetedTweet`, so we can
    unwrap it for free — no extra API call. That recovers the real post id, the
    real timestamp (an RT can be hours later, and hours can cross a date line),
    the real media and the real permalink. Retweets of OTHER handles are returned
    as-is; they are someone else's action and the parser drops them.
    """
    rt = getattr(tw, "retweetedTweet", None)
    if rt is not None and rt.user.username.lower() == username.lower():
        return rt, True
    return tw, False


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
    seen: set[int] = set()
    old_streak = 0
    scanned = 0
    unwrapped = 0
    dupes = 0

    async for tw in api.user_tweets(user.id, limit=limit):
        scanned += 1
        # Ordering guard uses the TIMELINE tweet's date; an unwrapped original is
        # older than its retweet and must not be read as "we've paged past since".
        if tw.date < since:
            old_streak += 1
            if old_streak >= STOP_AFTER_CONSECUTIVE_OLD:
                break
            continue
        old_streak = 0

        src, was_rt = _source_tweet(tw, username)
        if src.date < since:
            continue
        # The timeline re-serves pages (one @boyerBets_ post came back 33x), and
        # unwrapping can map a retweet onto an original we already have.
        if src.id in seen:
            dupes += 1
            continue
        seen.add(src.id)
        unwrapped += was_rt

        photos = [m.url for m in src.media.photos] if src.media else []
        videos = [m.thumbnailUrl for m in src.media.videos] if src.media else []
        results.append({
            "id": src.id,
            "date": src.date.astimezone(POST_TZ).isoformat(),
            "text": src.rawContent,
            "photos": "|".join(photos),
            "videos": "|".join(videos),
            "url": f"https://x.com/{src.user.username}/status/{src.id}",
        })

    results.sort(key=lambda r: r["id"], reverse=True)
    print(f"Scanned {scanned} timeline entries -> {len(results)} unique tweets "
          f"({unwrapped} self-retweets unwrapped to originals, {dupes} duplicates dropped)")
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
    parser.add_argument("--username", required=True, help="X handle, without the @")
    parser.add_argument("--since", required=True, help="YYYY-MM-DD cutoff date")
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
