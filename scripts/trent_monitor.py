"""
trent_monitor.py — Poll @BookitWithTrent for new picks, send to Telegram.

Designed to run every 5 minutes via systemd timer.

Flow:
  1. Fetch recent tweets via twscrape
  2. Filter out already-seen tweet IDs (stored in picks.db)
  3. Parse each new tweet with Claude (text + image fallback)
  4. Send picks to Telegram channel
  5. Mark all processed tweets as seen

Usage:
    python scripts/trent_monitor.py                  # run once (prod channel)
    python scripts/trent_monitor.py --dry-run        # parse only, don't send
    python scripts/trent_monitor.py --channel ID     # send to specific channel
"""

import asyncio
import base64
import io
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from twscrape import API

from telethon import TelegramClient
from telethon.sessions import StringSession

from ai import _claude_create_with_retry, usage_cost, fmt_cost

# ─── Config ──────────────────────────────────────────────────────────────────

USERNAME = "BookitWithTrent"
DEST_CHANNEL = -1004394797084
DB_PATH = str(ROOT / "picks.db")
# How far back to look for tweets each run (covers missed runs / gaps)
LOOKBACK_HOURS = 2

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]

# ─── DB ──────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trent_seen (
    tweet_id    TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    had_pick    INTEGER NOT NULL DEFAULT 0
);
"""


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute(_SCHEMA)
    con.commit()
    return con


def _get_seen(con: sqlite3.Connection) -> set[str]:
    """Return set of tweet IDs we've already processed."""
    rows = con.execute("SELECT tweet_id FROM trent_seen").fetchall()
    return {r[0] for r in rows}


def _mark_seen(con: sqlite3.Connection, tweet_id: str, had_pick: bool):
    con.execute(
        "INSERT OR IGNORE INTO trent_seen (tweet_id, processed_at, had_pick) VALUES (?, ?, ?)",
        (tweet_id, datetime.now(timezone.utc).isoformat(), int(had_pick)),
    )
    con.commit()


def _prune_old(con: sqlite3.Connection, days: int = 7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    con.execute("DELETE FROM trent_seen WHERE processed_at < ?", (cutoff,))
    con.commit()


# ─── Tweet fetching ──────────────────────────────────────────────────────────

async def _fetch_impl(since: datetime, limit: int) -> list[dict]:
    api = API()
    auth_token = os.environ.get("X_AUTH_TOKEN", "")
    ct0 = os.environ.get("X_CT0", "")
    if not auth_token or not ct0:
        print("ERROR: Set X_AUTH_TOKEN and X_CT0 in .env")
        return []

    await api.pool.add_account_cookies("me", f"auth_token={auth_token}; ct0={ct0}")
    user = await api.user_by_login(USERNAME)

    results = []
    old_streak = 0
    async for tw in api.user_tweets(user.id, limit=limit):
        if tw.date < since:
            old_streak += 1
            # Pinned tweets come first and may be old — skip them.
            # Stop after 3 consecutive old tweets (past the pinned ones).
            if old_streak >= 3:
                break
            continue
        old_streak = 0
        photos = [m.url for m in tw.media.photos] if tw.media else []
        results.append({
            "id": str(tw.id),
            "date": tw.date.isoformat(),
            "text": tw.rawContent,
            "photos": "|".join(photos),
            "url": f"https://x.com/{user.username}/status/{tw.id}",
        })

    return results


async def fetch_recent_tweets(since: datetime, limit: int = 50) -> list[dict]:
    """Fetch tweets with a timeout so we don't block when rate-limited."""
    try:
        return await asyncio.wait_for(_fetch_impl(since, limit), timeout=90)
    except asyncio.TimeoutError:
        print("  Rate-limited by Twitter, skipping this run")
        return []


# ─── Pick detection ──────────────────────────────────────────────────────────

_IS_PICK_PROMPT = """\
Is this tweet an OFFICIAL PICK PLACEMENT — a real wager being announced RIGHT NOW?

YES signals:
- "mortal mega" / "mortal mega max" / "mega" — signature bet branding
- "nuke" / "nuking" / "I'm nuking [team]" — placing a big bet
- "OFFICIAL PLAY:" / "BEST PLAY:"
- "I'm on [pick]" / "I'll be on" / "I will be taking" / "I'm riding with"
- "I have $X on [team]" / "dropping $X on"
- "10u" / "10 unit" / "$X,000" — unit/dollar sizing
- "play of year" / "square of year" / "play of the day"
- "FUGAZI 5" / "[N]-man nuke" / "Last Chance U slip" — named bet formats
- Explicit first-person declaration of placing a specific bet

NO — return false for:
- Celebrations/results: "BANGGGG", "✅✅✅", "CASH THE MORTAL MEGA", win announcements
- Loss reactions: "chalked", "GGs", "dead", "horrible wager"
- In-game commentary, hopes/wishes without placement
- Rhetorical/teasers: "I wanna nuke it so bad", "mortal megas?👀", "the next nuke is loading..."
- Off-topic: giveaways, streams, card ripping, podcast links
- Teaser without naming the pick: "I have the mortal mega. 😈" (no team/line = not a pick yet)
- Referencing a PAST bet: "I lost $X on...", "mom has no clue I have $5k on [team]"
- Corrections of a prior pick (not a new announcement)

The KEY distinction: the tweet must be the ORIGINAL ANNOUNCEMENT where the bettor declares they are placing the wager.

A tweet that mentions "3 STRAIGHT WINNERS ✅✅✅" at the top but then announces the NEXT pick below IS a pick.

Return only: true or false

Tweet:
{text}"""

_IMAGE_IS_PICK_PROMPT = """\
This tweet has pick signals in the text but the actual bet may be in the attached image (bet slip).

Does the image show a SINGLE sports bet being placed? Return false if:
- Multi-leg parlay (multiple bets on one slip)
- Multiple separate bets
- No bet slip / not a pick image

Tweet text: {text}

Return only: true or false"""


def _is_retweet(text: str) -> bool:
    return text.lstrip().startswith("RT @")


_PICK_SIGNALS = [
    "mortal mega", "mortal", "mega max", "nuke", "nuking",
    "official", "play of", "square of", "last chance u",
    "i'm on", "i'll be on", "i will be taking", "i'm riding",
    "i have $", "dropping", "10u", "10 unit",
    "slip", "fugazi",
]


def _has_pick_signal(text: str) -> bool:
    tl = text.lower()
    return any(s in tl for s in _PICK_SIGNALS)


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() == "true"


async def is_pick_text(tweet: dict) -> bool:
    """Ask Claude if this tweet is an official pick. Returns True/False."""
    text = tweet.get("text", "").strip()
    if not text or _is_retweet(text):
        return False
    try:
        resp = await _claude_create_with_retry(
            model="claude-sonnet-4-6",
            max_tokens=10,
            messages=[{"role": "user", "content": _IS_PICK_PROMPT.format(text=text)}],
        )
    except Exception as e:
        print(f"  ERROR text-check {tweet['id']}: {e}")
        return False
    return _parse_bool(resp.content[0].text)


async def download_image(url: str) -> tuple[str, bytes] | None:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "image/jpeg")
            return ct.split(";")[0].strip(), resp.content
    except Exception as e:
        print(f"    image download failed {url}: {e}")
        return None


async def is_pick_image(tweet: dict) -> bool:
    """Check if a tweet's image contains a single bet slip."""
    photos = tweet.get("photos", "").strip()
    if not photos:
        return False
    img_url = photos.split("|")[0].strip()
    if not img_url:
        return False

    img = await download_image(img_url)
    if not img:
        return False
    media_type, img_bytes = img
    img_b64 = base64.b64encode(img_bytes).decode()

    text = tweet.get("text", "").strip()
    try:
        resp = await _claude_create_with_retry(
            model="claude-sonnet-4-6",
            max_tokens=10,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": _IMAGE_IS_PICK_PROMPT.format(text=text)},
            ]}],
        )
    except Exception as e:
        print(f"  ERROR image-check {tweet['id']}: {e}")
        return False
    return _parse_bool(resp.content[0].text)


# ─── Telegram ────────────────────────────────────────────────────────────────

async def send_pick(tweet: dict, dest: int | str, dry_run: bool = False):
    """Send original tweet content (text + images) to Telegram channel."""
    text = tweet.get("text", "").strip()
    url = tweet["url"]
    msg = f"\u25fc\ufe0f Trent \u2022 {text}\n\n{url}"
    if dry_run:
        print(f"  [dry-run] Would send:\n    {msg[:120]}...")
        return

    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()
    try:
        entity = await client.get_entity(dest)
        # Download tweet images
        photos = tweet.get("photos", "").strip()
        photo_files = []
        if photos:
            for img_url in photos.split("|"):
                img_url = img_url.strip()
                if not img_url:
                    continue
                img = await download_image(img_url)
                if img:
                    buf = io.BytesIO(img[1])
                    buf.name = "photo.jpg"
                    photo_files.append(buf)

        if photo_files:
            await client.send_file(
                entity, photo_files, caption=msg, link_preview=False,
            )
        else:
            await client.send_message(entity, msg, link_preview=False)
        print(f"  Sent pick to {dest}")
    finally:
        await client.disconnect()


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Poll @BookitWithTrent for new picks")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, don't send to Telegram")
    parser.add_argument("--channel", type=int, default=DEST_CHANNEL, help="Destination Telegram channel ID")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_HOURS, help="Hours to look back for tweets")
    args = parser.parse_args()

    con = _db()
    _prune_old(con)
    seen = _get_seen(con)

    since = datetime.now(timezone.utc) - timedelta(hours=args.lookback)
    print(f"Fetching @{USERNAME} tweets since {since.strftime('%H:%M UTC')}...")
    tweets = await fetch_recent_tweets(since)
    print(f"  {len(tweets)} tweets fetched")

    # Filter to unseen
    new_tweets = [t for t in tweets if t["id"] not in seen]
    if not new_tweets:
        print("  No new tweets")
        con.close()
        return

    print(f"  {len(new_tweets)} new tweets to process")

    # Process oldest first so picks are sent in chronological order
    new_tweets.sort(key=lambda t: t["date"])

    picks_sent = 0
    for tw in new_tweets:
        is_pick = await is_pick_text(tw)

        # Image fallback: text has pick signals but Claude said no pick from text
        if not is_pick and tw.get("photos") and _has_pick_signal(tw.get("text", "")):
            is_pick = await is_pick_image(tw)

        if is_pick:
            await send_pick(tw, args.channel, dry_run=args.dry_run)
            picks_sent += 1

        _mark_seen(con, tw["id"], had_pick=is_pick)

    con.close()
    print(f"Done: {picks_sent} picks sent, {len(new_tweets)} tweets processed")
    cost = usage_cost()
    if cost > 0:
        print(f"[Claude cost] {fmt_cost(cost)}")


if __name__ == "__main__":
    asyncio.run(main())
