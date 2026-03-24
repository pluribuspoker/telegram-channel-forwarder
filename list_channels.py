from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel
import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]

with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
    for dialog in client.iter_dialogs():
        if isinstance(dialog.entity, Channel):
            print(f"{dialog.id:>20}  {dialog.name}")
