#!/usr/bin/env python3
"""Put a custom (Premium) emoji reaction on a message as @ForwarderClaudeBot.

Bots may only add a custom reaction that is already on the message, and a
group cannot whitelist specific custom reactions (tried 2026-09-26: Telegram
silently drops them; that is a channel-only feature). So:

  1. the operator's Premium session adds the custom reaction (seed),
  2. the bot adds the same one (Bot API setMessageReaction),
  3. the operator's session restores its previous reactions (unseed).

Only the bot's reaction remains; the operator's shows for ~1s. The operator
granted standing use of their session (2026-09-26). Each call is one short
Telethon connection — use sparingly, never in a loop (listener shares the key).

Private chats number messages per account, so a DM message id from the bot's
side is mapped to the operator's side by timestamp (--ts, the inbound <channel>
ts). Supergroup ids are shared by every member.

  python3 scripts/custom_react.py --chat 5911202683 --msg 1220 \\
      --ts 2026-09-27T02:36:35Z --emoji-id 5368562433981947135
"""
import argparse
import asyncio
import json
import os
import sys
import urllib.request
from datetime import datetime

from dotenv import load_dotenv

load_dotenv("/home/forwarder/app/.env")
load_dotenv("/home/forwarder/app/.env.local", override=True)

from telethon import TelegramClient, functions, types  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

BOT_ENV = os.path.expanduser("~/.claude/channels/telegram/.env")


def bot_token() -> str:
    for line in open(BOT_ENV, encoding="utf-8"):
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no TELEGRAM_BOT_TOKEN in " + BOT_ENV)


def bot_api(token: str, method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


async def run(args) -> int:
    token = bot_token()
    client = TelegramClient(
        StringSession(os.getenv("TELEGRAM_SESSION")),
        int(os.getenv("TELEGRAM_API_ID")),
        os.getenv("TELEGRAM_API_HASH"),
    )
    await client.connect()
    try:
        if args.chat > 0:
            # DM with the bot: the operator's side of the chat is the bot user.
            me = bot_api(token, "getMe", {})["result"]
            peer = await client.get_entity(me["username"])
            if not args.ts:
                raise SystemExit("--ts required for a private chat")
            ts = datetime.fromisoformat(args.ts.replace("Z", "+00:00"))
            user_msg = None
            async for m in client.iter_messages(peer, limit=50):
                if abs((m.date - ts).total_seconds()) <= 1 and m.out:
                    user_msg = m
                    break
            if user_msg is None:
                raise SystemExit(f"no operator message at {args.ts} in the bot DM")
        else:
            peer = await client.get_entity(args.chat)
            user_msg = await client.get_messages(peer, ids=args.msg)
            if user_msg is None:
                raise SystemExit(f"message {args.msg} not found")

        # The operator's own current reactions (chosen_order is set only on
        # theirs) — restored verbatim after the bot has copied the seed.
        chosen = [r for r in (user_msg.reactions.results if user_msg.reactions else [])
                  if r.chosen_order is not None]
        mine = [r.reaction for r in sorted(chosen, key=lambda r: r.chosen_order)]
        custom = types.ReactionCustomEmoji(document_id=args.emoji_id)
        await client(functions.messages.SendReactionRequest(
            peer=peer, msg_id=user_msg.id, reaction=mine + [custom]))
        try:
            res = bot_api(token, "setMessageReaction", {
                "chat_id": args.chat,
                "message_id": args.msg,
                "reaction": [{"type": "custom_emoji", "custom_emoji_id": str(args.emoji_id)}],
            })
        finally:
            await client(functions.messages.SendReactionRequest(
                peer=peer, msg_id=user_msg.id, reaction=mine))
        if not res.get("ok"):
            print(f"bot reaction failed: {res.get('description')}", file=sys.stderr)
            return 1
        print("reacted")
        return 0
    finally:
        await client.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chat", type=int, required=True, help="bot-side chat id")
    ap.add_argument("--msg", type=int, required=True, help="bot-side message id")
    ap.add_argument("--ts", help="message timestamp (ISO, UTC) — required for DMs")
    ap.add_argument("--emoji-id", type=int, required=True, help="custom emoji document id")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
