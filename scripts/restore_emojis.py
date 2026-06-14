"""One-off: restore result emojis cleared by clear_emojis.py.

Reads verdicts from parse_cache.json and re-inserts emojis using
_insert_emojis (which respects blockquote boundaries).
Does NOT reset broadcasted flags or trigger broadcast messages.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(".env")
load_dotenv(".env.local", override=True)

from telethon import TelegramClient
from telethon.sessions import StringSession

from tracker_format import _insert_emojis, _bot_edit_message, _user_edit_message

CHANNEL = -1002486251914

# Messages cleared by clear_emojis that need restoration (excluding 2949 already fixed)
MSG_IDS = [
    2965, 2964, 2963, 2962, 2961, 2960, 2959, 2958,
    2955, 2954, 2953, 2952, 2951, 2946, 2945, 2944,
    2943, 2941, 2940, 2939,
]


def _to_bot_html(text, entities):
    from telethon.extensions import html as tl_html
    ht = tl_html.unparse(text, entities or [])
    return ht.replace("<spoiler>", "<tg-spoiler>").replace("</spoiler>", "</tg-spoiler>")


async def main():
    cache = json.load(open("parse_cache.json"))

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session = os.getenv("TELEGRAM_SESSION", "")
    bot_token = os.getenv("BOT_TOKEN", "")

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        entity = await client.get_entity(CHANNEL)

        for mid in MSG_IDS:
            key = f"{CHANNEL}:{mid}"
            entry = cache.get(key)
            if not entry:
                print(f"  {mid}  SKIP (no cache entry)")
                continue

            leg_verdicts = entry.get("leg_verdicts", {})
            picks = entry.get("parsed", {}).get("picks", [])

            # Build verdicts list: (pick, verdict_str, calc, sport)
            verdicts = []
            for idx_str, vdata in sorted(leg_verdicts.items()):
                verdict = vdata.get("verdict")
                if verdict not in ("WIN", "LOSS", "PUSH"):
                    continue
                idx = int(idx_str)
                if idx < len(picks):
                    verdicts.append((
                        picks[idx],
                        verdict,
                        vdata.get("calc", ""),
                        vdata.get("sport", ""),
                    ))

            if not verdicts:
                print(f"  {mid}  SKIP (no graded verdicts)")
                continue

            # Fetch current message from Telegram
            msg = await client.get_messages(entity, ids=mid)
            if not msg or not msg.text:
                print(f"  {mid}  SKIP (message not found)")
                continue

            html_text = _to_bot_html(msg.text, msg.entities)
            new_text = _insert_emojis(html_text, verdicts)

            if new_text == html_text:
                print(f"  {mid}  SKIP (no change)")
                continue

            ok = await _bot_edit_message(bot_token, CHANNEL, mid, new_text, msg.media is not None)
            if ok:
                print(f"  {mid}  OK  ({entry.get('capper_name', '?')})")
            else:
                # fallback to user edit
                ok2 = await _user_edit_message(client, entity, mid, new_text)
                if ok2:
                    print(f"  {mid}  OK (user edit)  ({entry.get('capper_name', '?')})")
                else:
                    print(f"  {mid}  FAIL  ({entry.get('capper_name', '?')})")

            await asyncio.sleep(1)  # avoid flood


asyncio.run(main())
