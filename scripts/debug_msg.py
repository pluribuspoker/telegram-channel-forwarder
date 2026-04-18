"""Debug script to inspect message text and trace emoji insertion."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(".env")
load_dotenv(".env.local", override=True)

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session = os.getenv("TELEGRAM_SESSION", "")

    channel_id = -1002486251914
    msg_id = 2287

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        msg = await client.get_messages(channel_id, ids=msg_id)
        print("=== RAW TEXT ===")
        print(repr(msg.text))
        print()
        print("=== ENTITIES ===")
        for e in (msg.entities or []):
            print(e)
        print()

        # Convert to bot HTML
        from tracker import _to_bot_html
        html = _to_bot_html(msg.text, msg.entities)
        print("=== HTML ===")
        print(html)
        print()

        # Show lines
        lines = html.split("\n")
        for i, line in enumerate(lines):
            print(f"  [{i}] {repr(line)}")


asyncio.run(main())
