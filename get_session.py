from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("Enter your API_ID: "))
API_HASH = input("Enter your API_HASH: ")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\nYour session string (copy this):\n")
    print(client.session.save())
