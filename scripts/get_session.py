"""
Generate a Telegram user session string and save it to .env.local.

Run locally to set up local development sessions.
Run on the VPS to set up the production session (ties session to server IP).

Usage:
    python scripts/get_session.py
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

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    session = client.session.save()

env_local = _root / ".env.local"
lines = env_local.read_text().splitlines() if env_local.exists() else []
lines = [l for l in lines if not l.startswith("TELEGRAM_SESSION=")]
lines.append(f'TELEGRAM_SESSION="{session}"')
env_local.write_text("\n".join(lines) + "\n")

print(f"Session saved to {env_local}")
