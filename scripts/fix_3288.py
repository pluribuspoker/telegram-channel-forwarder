"""Fix emoji placement on message 3288: move ❌ from capper line to pick line."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(".env")
load_dotenv(".env.local", override=True)

from tracker_format import _bot_edit_message

CHANNEL = -1002486251914
MSG_ID = 3288

# Correct caption: emoji on the pick line, not the capper line
NEW_CAPTION = (
    "Soccer Guru (17-10 BTTS) &amp; Sharp Syndicate (24-9 BTTS) both have...\n"
    "\n"
    "Switzerland vs Colombia BOTH TO SCORE - YES\u274c\n"
    "\n"
    "They both had BTTS in Portugal / Spain game which ended 1:0 so... "
    "just sharing the stats I guess. Personally will lay this one off for a live bet"
)

async def main():
    bot_token = os.environ["BOT_TOKEN"]
    ok = await _bot_edit_message(bot_token, CHANNEL, MSG_ID, NEW_CAPTION, has_media=True)
    print("OK" if ok else "FAILED")

asyncio.run(main())
