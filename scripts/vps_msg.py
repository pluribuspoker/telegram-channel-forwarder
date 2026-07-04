"""Fetch a Telegram message by channel ID and message ID.

Usage (on VPS as forwarder):
    ~/venv/bin/python scripts/vps_msg.py <channel_id> <msg_id>

Example:
    ~/venv/bin/python scripts/vps_msg.py -1002486251914 3243
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
load_dotenv(".env")
load_dotenv(".env.local", override=True)

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <channel_id> <msg_id>")
        sys.exit(1)

    channel_id = int(sys.argv[1])
    msg_id = int(sys.argv[2])

    client = TelegramClient(
        StringSession(os.environ["TELEGRAM_SESSION"]),
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
    )
    await client.start()
    msg = await client.get_messages(channel_id, ids=msg_id)
    if not msg:
        print("Message not found")
        await client.disconnect()
        return

    print(f"TEXT: {msg.text!r}")
    print(f"DATE: {msg.date}")
    print(f"SENDER: {msg.sender_id}")
    print(f"MEDIA: {type(msg.media).__name__ if msg.media else None}")
    if msg.reply_to:
        print(f"REPLY_TO: {msg.reply_to.reply_to_msg_id}")
    if msg.entities:
        for e in msg.entities:
            print(f"ENTITY: {type(e).__name__} {e}")
    await client.disconnect()

asyncio.run(main())
