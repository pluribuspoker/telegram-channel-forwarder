"""Show content and date of specific messages."""
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

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        for mid in [2234, 2236]:
            msg = await client.get_messages(-1002486251914, ids=mid)
            if not msg:
                continue
            date = msg.date.strftime("%Y-%m-%d %H:%M UTC")
            print(f"=== Message {mid} ({date}) ===")
            print(msg.text)
            print()


asyncio.run(main())
