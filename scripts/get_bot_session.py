"""
Run this script ON THE VPS to generate a persistent bot session string.
The session ties to the server IP so Telegram won't create a new login on each restart.

Usage (on VPS):
    su - forwarder
    cd ~/app
    ~/venv/bin/python scripts/get_bot_session.py

Then append the output to .env.local:
    echo 'BOT_SESSION="<session string>"' >> /home/forwarder/app/.env.local
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

_root = Path(__file__).parent.parent
load_dotenv(_root / ".env")
load_dotenv(_root / ".env.local", override=True)

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    client.start(bot_token=BOT_TOKEN)  # non-interactive when bot_token is provided
    print("\nBot session string (copy this):\n")
    print(client.session.save())
    print()

