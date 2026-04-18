"""One-off script to fix wrong emojis on message 2287."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(".env")
load_dotenv(".env.local", override=True)

from tracker_format import _insert_emojis, _insert_odds, strip_label
from tracker_cache import _load_pending_cache, _save_pending_cache


async def main():
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from tracker import _to_bot_html
    from tracker_format import _bot_edit_message
    import re

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session = os.getenv("TELEGRAM_SESSION", "")
    bot_token = os.getenv("BOT_TOKEN", "")

    channel_id = -1002486251914
    msg_id = 2287
    cache_key = f"{channel_id}:{msg_id}"

    # Load parse cache
    cache = _load_pending_cache()
    entry = cache.get(cache_key, {})
    picks = entry.get("parsed", {}).get("picks", [])
    odds = entry.get("odds_by_pick", {})
    leg_verdicts = entry.get("leg_verdicts", {})

    print(f"Picks: {len(picks)}")
    for i, p in enumerate(picks):
        v = leg_verdicts.get(str(i), {})
        print(f"  [{i}] {p.get('description')} → {v.get('verdict')} (broadcast={v.get('broadcasted')})")

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        msg = await client.get_messages(channel_id, ids=msg_id)
        print(f"\nCurrent text:\n{msg.text}\n")

        # Strip existing emojis from plain text
        clean_text = re.sub(r'[\u2705\u274c\u267b\ufe0f]', '', msg.text)
        print(f"Clean text:\n{clean_text}\n")

        # Rebuild entities from original message but with clean text
        # We need to re-read the message to get proper entities for clean text
        # Instead: use the clean text and the blockquote entity adjusted for removed chars
        html = _to_bot_html(clean_text, None)  # No entities - will lose blockquote

        # Better approach: build from clean text + known blockquote
        # Find the stats section and wrap in blockquote
        lines = clean_text.split("\n")
        html_lines = []
        in_blockquote = False
        for line in lines:
            if line.strip().startswith("UFL"):
                html_lines.append(f"<blockquote>{line}")
                in_blockquote = True
            elif in_blockquote:
                html_lines.append(line)
            else:
                html_lines.append(line)
        html = "\n".join(html_lines)
        if in_blockquote:
            html = html.rstrip() + "</blockquote>"
        html = html.replace(">=", "&gt;=")

        print(f"Clean HTML:\n{html}\n")

        # Insert odds
        html = _insert_odds(html, picks, odds)
        print(f"After odds:\n{html}\n")

        # Build verdicts
        verdicts = []
        for i, pick in enumerate(picks):
            v = leg_verdicts.get(str(i), {})
            verdicts.append((pick, v.get("verdict", "UNKNOWN"), v.get("calc", ""), v.get("sport", "")))

        # Insert emojis
        html = _insert_emojis(html, verdicts)
        print(f"Final HTML:\n{html}\n")

        # Verify
        assert "[-132]❌" in html, "Stallions should have ❌"
        assert "[-165]✅" in html, "Defenders should keep ✅"
        assert "Fav ML</blockquote>" in html, "Stats should be clean"
        print("✓ Verification passed!")

        # Edit the message
        if "--apply" in sys.argv:
            ok = await _bot_edit_message(bot_token, channel_id, msg_id, html, msg.media is not None)
            if ok:
                print("\n✓ Message edited successfully!")
                # Update cache: mark both as broadcasted
                for i in range(len(picks)):
                    if str(i) in leg_verdicts:
                        leg_verdicts[str(i)]["broadcasted"] = True
                _save_pending_cache(cache)
                print("✓ Cache updated")
            else:
                print("\n✗ Edit failed!")
        else:
            print("\nDry run — pass --apply to actually edit the message")


asyncio.run(main())
