"""
Generate a persistent bot session string and save it to .env.local.

Run locally for local dev. Run on the VPS for production (ties session to server IP).

Usage:
    python scripts/get_bot_session.py
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
    client.start(bot_token=BOT_TOKEN)
    session = client.session.save()

env_local = _root / ".env.local"
lines = env_local.read_text().splitlines() if env_local.exists() else []
lines = [l for l in lines if not l.startswith("BOT_SESSION=")]
lines.append(f'BOT_SESSION="{session}"')
env_local.write_text("\n".join(lines) + "\n")

print(f"Bot session saved to {env_local}")
