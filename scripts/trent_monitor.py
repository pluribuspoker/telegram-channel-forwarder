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
import json
import os
import re
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

async def fetch_recent_tweets(since: datetime, limit: int = 50) -> list[dict]:
    api = API()
    auth_token = os.environ.get("X_AUTH_TOKEN", "")
    ct0 = os.environ.get("X_CT0", "")
    if not auth_token or not ct0:
        print("ERROR: Set X_AUTH_TOKEN and X_CT0 in .env")
        return []

    await api.pool.add_account_cookies("me", f"auth_token={auth_token}; ct0={ct0}")
    user = await api.user_by_login(USERNAME)

    results = []
    async for tw in api.user_tweets(user.id, limit=limit):
        if tw.date < since:
            break
        photos = [m.url for m in tw.media.photos] if tw.media else []
        results.append({
            "id": str(tw.id),
            "date": tw.date.isoformat(),
            "text": tw.rawContent,
            "photos": "|".join(photos),
            "url": f"https://x.com/{user.username}/status/{tw.id}",
        })

    return results


# ─── Pick parsing (reuses parse_posts_csv prompts) ──────────────────────────

_PICK_PROMPT = """\
You are analyzing a tweet from a sports bettor to determine if it contains an OFFICIAL PICK PLACEMENT — a real wager being announced.

{date_context}
STEP 1: Is this an official pick ANNOUNCEMENT?

An official pick is a tweet where the bettor is ANNOUNCING a wager they are placing RIGHT NOW. Look for these signals:
- "mortal mega" / "mortal mega max" / "mega" — signature bet branding
- "nuke" / "nuking" / "I'm nuking [team]" — placing a big bet
- "OFFICIAL PLAY:" / "BEST PLAY:" — designates the pick in multi-prediction posts
- "I'm on [pick]" / "I'll be on" / "I will be taking" / "I'm riding with"
- "I have $X on [team]" / "dropping $X on"
- "10u" / "10 unit" / "$X,000" — unit/dollar sizing
- "play of year" / "square of year" / "play of the day"
- "FUGAZI 5" / "[N]-man nuke" — named parlay formats
- "Last Chance U slip" — named bet format
- Explicit first-person declaration of placing a specific bet with a named team/side/line

NOT an official pick (return null):
- Celebrations/results: "BANGGGG", "✅✅✅", "CASH THE MORTAL MEGA", "howsya", win announcements
- Loss reactions: "chalked", "GGs", "dead", "fuck", "horrible wager", "🥀", "❌"
- In-game commentary: "if this goes over", "they ain't scoring", "I wanna throw up"
- Hopes/wishes without placement: "Life on line, Mbappe is NOT scoring", "will do anything for an Austria bang"
- Rhetorical/teasers: "why shouldn't I just put...", "I wanna nuke it so bad", "mortal megas?👀", "the next nuke is loading..."
- Off-topic: giveaways, streams, card ripping, podcast links
- Pure excitement about a game event: "CEASE NO HITTER" x3 (celebrating in progress, not a bet)
- Score updates, goal celebrations, general commentary
- Teaser without naming the pick: "I have the mortal mega" / "I have the mortal mega. 😈" (no team/line named = not a pick yet)
- Referencing a PAST or EXISTING bet without announcing it: "I lost $X on...", "if you took [team] you're sharp", "mom has no clue I have $5k on [team]", "I will tell my grandchildren about [pick]" — these mention a bet but are NOT the original announcement
- Corrections or clarifications of a PRIOR pick: "AI switched the wording, this is [team] +1.5" — this is clarifying a pick already announced in an earlier tweet, not a new pick

The KEY distinction: the tweet must be the ORIGINAL ANNOUNCEMENT where the bettor declares they are placing the wager. Tweets that reference, joke about, reminisce about, or correct an already-announced bet are NOT picks.

A tweet that mentions "3 STRAIGHT WINNERS ✅✅✅" at the top but then announces the NEXT pick below is still a pick placement — the ✅ line is context, the actual pick follows.

STEP 2: If the post IS an official pick, extract exactly ONE pick.

If the post has "OFFICIAL PLAY:" or "BEST PLAY:", extract ONLY that designated pick, ignore the score predictions and other sides listed.

If the post announces a single bet (e.g. "Germany -1.5 for a mortal mega"), extract that.

If the post is a named parlay (FUGAZI 5, 4-man nuke, etc.) with multiple legs, return null — we only want single-game picks.

Return JSON (no markdown fences). Return null if no official pick or if the pick is a multi-leg parlay:

{{
  "sport": "NBA|NCAAB|MLB|NFL|NHL|UFL|CFL|Tennis|UFC|Boxing|KBO|Soccer|Other",
  "pick": {{
    "description": "concise one-line summary of the exact bet"
  }}
}}

Tweet:
{text}"""

_IMAGE_PROMPT = """\
This tweet from a sports bettor contains a bet slip or wager image. \
The tweet text alone does not specify the exact pick — it is shown in the attached image.

{date_context}
Extract the SINGLE official pick from the image. If the image shows a multi-leg parlay \
(multiple bets combined into one slip), return null — we only want single-game picks. \
If the image shows multiple separate single bets, return null.

Tweet text: {text}

Return JSON (no markdown fences). Return null if multi-leg parlay or no clear single pick:

{{
  "sport": "NBA|NCAAB|MLB|NFL|NHL|UFL|CFL|Tennis|UFC|Boxing|KBO|Soccer|Other",
  "pick": {{
    "description": "concise one-line summary of the exact bet"
  }}
}}"""

_PICK_SIGNALS = [
    "mortal mega", "mortal", "mega max", "nuke", "nuking",
    "official", "play of", "square of", "last chance u",
    "i'm on", "i'll be on", "i will be taking", "i'm riding",
    "i have $", "dropping", "10u", "10 unit",
    "slip", "fugazi",
]


def _date_ctx(date_str: str) -> str:
    if not date_str:
        return ""
    from datetime import date as _d
    d = _d.fromisoformat(date_str[:10])
    return f"Context: Today is {d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}.\n"


def _extract_result(raw_text: str) -> dict | None:
    raw = re.sub(r"^```(?:json)?\n?|```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    if raw.lower() == "null":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not parsed or "pick" not in parsed or parsed["pick"] is None:
        return None
    return parsed


def _has_pick_signal(text: str) -> bool:
    tl = text.lower()
    return any(s in tl for s in _PICK_SIGNALS)


def _is_retweet(text: str) -> bool:
    return text.lstrip().startswith("RT @")


async def parse_tweet_text(tweet: dict) -> dict | None:
    """Try to extract a pick from tweet text. Returns {description, url} or None."""
    text = tweet.get("text", "").strip()
    if not text or _is_retweet(text):
        return None
    try:
        resp = await _claude_create_with_retry(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": _PICK_PROMPT.format(
                text=text, date_context=_date_ctx(tweet.get("date", ""))
            )}],
        )
    except Exception as e:
        print(f"  ERROR text-parse {tweet['id']}: {e}")
        return None
    parsed = _extract_result(resp.content[0].text)
    if not parsed:
        return None
    return {"description": parsed["pick"]["description"], "url": tweet["url"]}


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


async def parse_tweet_image(tweet: dict) -> dict | None:
    """Try to extract a pick from tweet image. Returns {description, url} or None."""
    photos = tweet.get("photos", "").strip()
    if not photos:
        return None
    img_url = photos.split("|")[0].strip()
    if not img_url:
        return None

    img = await download_image(img_url)
    if not img:
        return None
    media_type, img_bytes = img
    img_b64 = base64.b64encode(img_bytes).decode()

    text = tweet.get("text", "").strip()
    try:
        resp = await _claude_create_with_retry(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": _IMAGE_PROMPT.format(
                    text=text, date_context=_date_ctx(tweet.get("date", ""))
                )},
            ]}],
        )
    except Exception as e:
        print(f"  ERROR image-parse {tweet['id']}: {e}")
        return None
    parsed = _extract_result(resp.content[0].text)
    if not parsed:
        return None
    return {"description": parsed["pick"]["description"], "url": tweet["url"]}


# ─── Telegram ────────────────────────────────────────────────────────────────

async def send_pick(pick: dict, dest: int | str, dry_run: bool = False):
    """Send a pick to Telegram channel."""
    msg = f"TRENT\n\n{pick['description']}\n{pick['url']}"
    if dry_run:
        print(f"  [dry-run] Would send:\n    {msg}")
        return

    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()
    try:
        entity = await client.get_entity(dest)
        await client.send_message(entity, msg)
        print(f"  Sent pick to {dest}")
    finally:
        await client.disconnect()


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Poll @BookitWithTrent for new picks")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, don't send to Telegram")
    parser.add_argument("--channel", type=int, default=DEST_CHANNEL, help="Destination Telegram channel ID")
    args = parser.parse_args()

    con = _db()
    _prune_old(con)
    seen = _get_seen(con)

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
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
        # Phase 1: text parse
        pick = await parse_tweet_text(tw)

        # Phase 2: image parse if text had no pick but has images + signals
        if not pick and tw.get("photos") and _has_pick_signal(tw.get("text", "")):
            pick = await parse_tweet_image(tw)

        if pick:
            await send_pick(pick, args.channel, dry_run=args.dry_run)
            picks_sent += 1

        _mark_seen(con, tw["id"], had_pick=bool(pick))

    con.close()
    print(f"Done: {picks_sent} picks sent, {len(new_tweets)} tweets processed")
    cost = usage_cost()
    if cost > 0:
        print(f"[Claude cost] {fmt_cost(cost)}")


if __name__ == "__main__":
    asyncio.run(main())
