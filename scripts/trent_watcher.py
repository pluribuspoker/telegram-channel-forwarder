"""
trent_watcher.py — Poll @BookitWithTrent for new picks, send to Telegram.

Designed to run every 15 minutes via systemd timer.

Flow:
  1. Fetch recent tweets via twscrape
  2. Filter out already-seen tweet IDs (stored in picks.db)
  3. Parse each new tweet with Claude (text + image fallback)
  4. Send picks to Telegram channel
  5. Mark all processed tweets as seen

Usage:
    python scripts/trent_watcher.py                  # run once (prod channel)
    python scripts/trent_watcher.py --dry-run        # parse only, don't send
    python scripts/trent_watcher.py --channel ID     # send to specific channel
"""

import asyncio
import base64
import io
import json
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

from scripts.x_client import XCredentialsError, build_api

from telethon import TelegramClient
from telethon.sessions import StringSession

from ai import _claude_create_with_retry, usage_cost, fmt_cost

# ─── Config ──────────────────────────────────────────────────────────────────

USERNAME = "BookitWithTrent"
DEST_CHANNEL = -1004394797084
DB_PATH = str(ROOT / "picks.db")
# How far back to look for tweets each run (covers missed runs / gaps)
LOOKBACK_HOURS = 2
# Pick classifier: temperature=0 so the same tweet gets the same verdict every
# run (temp defaults to 1.0 = flaky). Model must be one that still accepts a
# temperature param — Sonnet 5 / Opus 4.7+ / Fable 5 REJECT it (400). Sonnet 4.6
# keeps both strong judgment and temperature support, so it's the right pick.
CLASSIFY_MODEL = "claude-sonnet-4-6"


class _XAuthError(Exception):
    """X/Twitter credentials are missing or rejected.

    Fatal, not transient: every run fetches 0 tweets until a human pastes fresh
    cookies, so this must exit non-zero and page the operator rather than look
    like a quiet "no new tweets" day.
    """


class _ClassifyError(Exception):
    """Transient failure classifying a tweet (API/network/image download).

    Raised (not returned as False) so main() can SKIP marking the tweet seen
    and retry it next run — a transient blip must never permanently drop a pick.
    """

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
    try:
        api = await build_api()
    except XCredentialsError as e:
        raise _XAuthError(str(e)) from e

    user = await api.user_by_login(USERNAME)
    if user is None:
        # twscrape returns None when X rejects the cookies (expired/revoked).
        raise _XAuthError(
            f"X rejected the cookies — could not resolve @{USERNAME}. "
            "Refresh X_AUTH_TOKEN / X_CT0 from the browser."
        )

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


# State for alert rate-limiting, kept outside the repo (same spot as mem_watchdog).
_ALERT_STATE = Path.home() / ".trent_watcher_state.json"
_ALERT_EVERY_HOURS = 6


def _alert_operator(text: str) -> None:
    """DM the operator via the watchdog bot, at most once per _ALERT_EVERY_HOURS.

    Rate-limited so a multi-day credential outage doesn't DM every 15 minutes
    (and so the runner's built-in retry doesn't double-send).
    """
    now = datetime.now(timezone.utc)
    try:
        state = json.loads(_ALERT_STATE.read_text()) if _ALERT_STATE.exists() else {}
    except Exception:
        state = {}

    last = state.get("last_auth_alert")
    if last:
        try:
            if (now - datetime.fromisoformat(last)) < timedelta(hours=_ALERT_EVERY_HOURS):
                print("  (operator already alerted recently, not re-sending)")
                return
        except Exception:
            pass

    token = os.environ.get("WATCHDOG_BOT_TOKEN", "")
    uid = os.environ.get("WATCHDOG_USER_ID", "")
    if not token or not uid:
        print("  WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID not set, cannot alert", file=sys.stderr)
        return

    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": uid, "text": text},
            timeout=20,
        )
        if r.status_code != 200:
            print(f"  alert send failed: HTTP {r.status_code}", file=sys.stderr)
            return
    except Exception as e:
        print(f"  alert send failed: {e}", file=sys.stderr)
        return

    state["last_auth_alert"] = now.isoformat()
    try:
        tmp = _ALERT_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, _ALERT_STATE)
    except Exception as e:
        print(f"  could not persist alert state: {e}", file=sys.stderr)


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
- Terse pick announcement: "[team/player] ML", "[team/player] moneyline", "[team/player] +/-spread" — naming a specific bet even without fanfare
- Short tweet stating a pick with a bet type (ML, spread, over, under, total, 1H, o/u, BTTS / both teams to score, corners, cards) counts as an announcement
- A named matchup ("[Team] x [Team]" / "[Team] vs [Team]") paired with "mortal mega" / "mega" / "nuke" branding IS a single-game pick announcement, even if the specific bet lives in an attached slip image

NO — return false for:
- Multi-leg parlays: "FUGAZI 5", "[N]-man nuke" with multiple legs listed, "Last Chance U slip" with multiple legs — we only want SINGLE-GAME bets
- Celebrations/results: "BANGGGG", "✅✅✅", "CASH THE MORTAL MEGA", win announcements
- Loss reactions: "chalked", "GGs", "dead", "horrible wager"
- In-game commentary, hopes/wishes without placement
- Rhetorical/teasers: "I wanna nuke it so bad", "mortal megas?👀", "the next nuke is loading..."
- Off-topic: giveaways, streams, card ripping, podcast links
- Teaser without naming the pick: "I have the mortal mega. 😈" (no team/line = not a pick yet)
- Referencing a PAST bet: "I lost $X on...", "mom has no clue I have $5k on [team]"
- Corrections of a prior pick (not a new announcement)

The KEY distinction: the tweet must be the ORIGINAL ANNOUNCEMENT where the bettor declares they are placing the wager. Even a very short/casual tweet counts if it names a specific team/player and bet type.

A tweet that mentions "3 STRAIGHT WINNERS ✅✅✅" at the top but then announces the NEXT pick below IS a pick.

Return only: true or false

Tweet:
{text}"""

_IMAGE_IS_PICK_PROMPT = """\
This tweet has an attached image. Does it show a SINGLE sports bet being placed (bet slip, wager confirmation)?

Return true if the image shows a single-game bet slip with real money.

Return false if:
- Multi-leg parlay (multiple bets on one slip)
- Multiple separate bets
- No bet slip / not a pick image

Tweet text: {text}

Return only: true or false"""


def _is_retweet(text: str) -> bool:
    return text.lstrip().startswith("RT @")



def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() == "true"


async def is_pick_text(tweet: dict) -> bool:
    """Ask Claude if this tweet is an official pick. Returns True/False."""
    text = tweet.get("text", "").strip()
    if not text or _is_retweet(text):
        return False
    try:
        resp = await _claude_create_with_retry(
            model=CLASSIFY_MODEL,
            max_tokens=10,
            temperature=0,
            messages=[{"role": "user", "content": _IS_PICK_PROMPT.format(text=text)}],
        )
    except Exception as e:
        print(f"  ERROR text-check {tweet['id']}: {e}")
        raise _ClassifyError(str(e)) from e
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
        # Download failure is transient — don't let it silently drop a pick.
        raise _ClassifyError(f"image download failed for {tweet['id']}")
    media_type, img_bytes = img
    img_b64 = base64.b64encode(img_bytes).decode()

    text = tweet.get("text", "").strip()
    try:
        resp = await _claude_create_with_retry(
            model=CLASSIFY_MODEL,
            max_tokens=10,
            temperature=0,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": _IMAGE_IS_PICK_PROMPT.format(text=text)},
            ]}],
        )
    except Exception as e:
        print(f"  ERROR image-check {tweet['id']}: {e}")
        raise _ClassifyError(str(e)) from e
    return _parse_bool(resp.content[0].text)


# ─── Telegram ────────────────────────────────────────────────────────────────

def _strip_tco(text: str) -> str:
    """Remove trailing t.co media links from tweet text."""
    import re
    return re.sub(r'\s*https://t\.co/\S+', '', text).strip()


async def send_pick(tweet: dict, dest: int | str, dry_run: bool = False):
    """Send original tweet content (text + images) to Telegram channel."""
    import html as _html
    text = _strip_tco(tweet.get("text", "").strip())
    url = tweet["url"]
    # Variant #2: hide the raw URL behind a "\ud83d\udd17 View on X" footer link.
    # Tweet text is plain (no Telegram entities to preserve), so escape it for HTML.
    msg = (
        f"\u25fc\ufe0f Trent\n\n{_html.escape(text)}\n\n"
        f'<a href="{_html.escape(url, quote=True)}">\U0001f517 View on X</a>'
    )
    if dry_run:
        print(f"  [dry-run] Would send:\n    {msg[:160]}...")
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
                entity, photo_files, caption=msg, parse_mode="html",
                link_preview=False,
            )
        else:
            await client.send_message(
                entity, msg, parse_mode="html", link_preview=False,
            )
        print(f"  Sent pick to {dest}")
    finally:
        await client.disconnect()


async def _trigger_tracker_soon(channel):
    """Fire a quick tracker run to pull odds into freshly-sent Trent picks
    near-instantly, instead of waiting up to 5 min for the next timer cycle.

    Mirrors listener._trigger_tracker_soon, but scoped to the Trent channel so
    it stays fast (a few seconds) instead of grading every channel. This runs
    inside a oneshot systemd service, so we must AWAIT the subprocess — a
    fire-and-forget child would be killed when the service's main process exits
    (KillMode=control-group)."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for attempt in range(2):
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "tracker.py", "--live", "--days", "0.1",
                "--channel", str(channel),
                cwd=repo_root,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0 and attempt == 0:
                print(f"  [trigger] tracker quick-run exited {proc.returncode}, retrying in 5s")
                await asyncio.sleep(5)
                continue
        except Exception as e:
            print(f"  [trigger] tracker quick-run failed: {e}")
            return
        return


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
    try:
        tweets = await fetch_recent_tweets(since)
    except _XAuthError as e:
        # Hard stop: silently fetching 0 tweets forever is how a 2-day outage
        # hides behind "No new tweets" + a green systemd status.
        con.close()
        print(f"FATAL: {e}", file=sys.stderr)
        _alert_operator(
            f"🔴 Trent watcher is DOWN — no picks are being forwarded.\n\n"
            f"{e}\n\n"
            f"Fix: put X_AUTH_TOKEN / X_CT0 in /home/forwarder/app/.env.local, "
            f"then: sudo systemctl start trent-monitor.service"
        )
        sys.exit(1)
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
        try:
            is_pick = await is_pick_text(tw)

            # Image fallback: always check images if text check said no pick
            if not is_pick and tw.get("photos"):
                is_pick = await is_pick_image(tw)
        except _ClassifyError as e:
            # Transient blip — leave the tweet UNSEEN so it retries next run
            # (bounded: it stops being fetched once it ages out of the window).
            print(f"  transient classify failure on {tw['id']}, will retry next run: {e}")
            continue

        if is_pick:
            await send_pick(tw, args.channel, dry_run=args.dry_run)
            picks_sent += 1

        # A dry run must leave no trace: marking seen here would permanently
        # suppress the tweet from every later real run, so `--dry-run` would
        # silently destroy the thing it was meant to preview.
        if not args.dry_run:
            _mark_seen(con, tw["id"], had_pick=is_pick)

    con.close()
    print(f"Done: {picks_sent} picks sent, {len(new_tweets)} tweets processed")

    # Pull odds into the freshly-sent picks now instead of waiting up to 5 min
    # for the next tracker timer cycle. Only worth it if we actually sent picks.
    if picks_sent and not args.dry_run:
        print("  Triggering tracker quick-run for odds...")
        await _trigger_tracker_soon(args.channel)

    cost = usage_cost()
    if cost > 0:
        print(f"[Claude cost] {fmt_cost(cost)}")


if __name__ == "__main__":
    asyncio.run(main())
