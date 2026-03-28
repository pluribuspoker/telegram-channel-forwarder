#!/usr/bin/env python3
"""
clear_emojis.py — Strip verdict emojis from graded pick messages so the
tracker can re-grade them. Useful for testing.

Usage:
  python scripts/clear_emojis.py                    # today's messages
  python scripts/clear_emojis.py --days 2           # last 2 days
  python scripts/clear_emojis.py --channel -1002486251914
  python scripts/clear_emojis.py --dry-run          # preview only
"""

import asyncio
import argparse
import json
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.extensions import html as tl_html

load_dotenv()
load_dotenv(".env.local", override=True)

# All verdict emojis to strip
_VERDICT_EMOJIS = ["✅", "❌", "↩️", "❓", "⏳"]
_EMOJI_PAT = re.compile(r"\s*[" + "".join(_VERDICT_EMOJIS) + r"]+")


def _strip_emojis(text: str) -> str:
    """Remove verdict emojis (and surrounding whitespace) from text."""
    return _EMOJI_PAT.sub("", text).rstrip()


async def _bot_edit(bot_token: str, channel_id: int, message_id: int, text: str, has_media: bool) -> bool:
    method = "editMessageCaption" if has_media else "editMessageText"
    field  = "caption"            if has_media else "text"
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(
            f"https://api.telegram.org/bot{bot_token}/{method}",
            json={"chat_id": channel_id, "message_id": message_id,
                  field: text, "parse_mode": "HTML"},
        )
        if not r.is_success:
            body = r.json()
            desc = body.get("description", r.text)
            if "message is not modified" in desc:
                return True  # already clean
            print(f"  [error] {r.status_code}: {desc[:100]}")
            return False
        return True


async def run(days: int, channel: int | None, dry_run: bool) -> None:
    import datetime as dt

    api_id    = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash  = os.getenv("TELEGRAM_API_HASH", "")
    session   = os.getenv("TELEGRAM_SESSION", "")
    bot_token = os.getenv("BOT_TOKEN", "")

    channel_ids = json.loads(os.getenv("GRADE_CHANNELS", "[]"))
    if channel is not None:
        channel_ids = [channel]

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    mode   = "DRY RUN" if dry_run else "LIVE"
    print(f"\nClear emojis — {mode} | last {days} day(s) | channels: {channel_ids}\n")

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        for channel_id in channel_ids:
            entity = await client.get_entity(channel_id)
            ch_name = getattr(entity, "title", str(channel_id))
            print(f"{ch_name} ({channel_id}):")

            cleared = skipped = errors = 0
            async for msg in client.iter_messages(channel_id, limit=500):
                if msg.date < cutoff:
                    break
                text = msg.text or ""
                if not text:
                    continue

                # Skip messages with no verdict emojis
                if not any(em in text for em in _VERDICT_EMOJIS):
                    continue

                html_text = tl_html.unparse(text, msg.entities or [])
                html_text = html_text.replace("<spoiler>", "<tg-spoiler>").replace("</spoiler>", "</tg-spoiler>")
                clean = _strip_emojis(html_text)

                if clean == html_text:
                    skipped += 1
                    continue

                preview = text.splitlines()[0][:60]
                print(f"  [{'DRY' if dry_run else 'CLR'}]  {msg.id}  {preview}")

                if not dry_run:
                    ok = await _bot_edit(bot_token, channel_id, msg.id, clean, msg.media is not None)
                    if ok:
                        cleared += 1
                        await asyncio.sleep(0.3)
                    else:
                        errors += 1
                else:
                    cleared += 1

            print(f"  ─── cleared: {cleared}  skipped: {skipped}  errors: {errors}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Strip verdict emojis from pick messages")
    parser.add_argument("--days",    type=int, default=1, help="Days back to scan (default: 1)")
    parser.add_argument("--channel", type=int, metavar="ID", help="Limit to a single channel ID")
    parser.add_argument("--dry-run", action="store_true", help="Preview without editing")
    args = parser.parse_args()
    asyncio.run(run(days=args.days, channel=args.channel, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
