#!/usr/bin/env python3
"""Generate the dedicated intake bot's persistent Telethon session."""

import os
from pathlib import Path

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
token = os.environ["INTAKE_BOT_TOKEN"]

client = TelegramClient(StringSession(), api_id, api_hash)
client.start(bot_token=token)
session = client.session.save()
client.disconnect()

env_local = ROOT / ".env.local"
lines = env_local.read_text().splitlines() if env_local.exists() else []
lines = [
    line
    for line in lines
    if not line.startswith("INTAKE_BOT_SESSION=")
]
lines.append(f'INTAKE_BOT_SESSION="{session}"')
env_local.write_text("\n".join(lines) + "\n")
env_local.chmod(0o600)

print(f"Intake bot session saved to {env_local}")
