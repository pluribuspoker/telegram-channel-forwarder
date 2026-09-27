#!/usr/bin/env python3
"""Idempotently patch the Telegram channel plugin for custom-emoji reactions.

The stock plugin hardcodes `{type: 'emoji'}` in its react tool, so the session
can only use Telegram's ~70 standard reactions. This adds:

  * react: optional `custom_emoji_id` → `{type: 'custom_emoji'}` reaction
  * custom_emoji tool: list the custom reactions a chat allows (id + the
    standard emoji each one stands for), or every emoji in a custom-emoji pack

Bots may use a custom reaction only where the chat's admins allow it (or it is
already on the message) — Premium on the operator's account does not transfer.

Run by deploy/run_claude_channels.sh before every launch, so a plugin update
(new cache dir, stock server.ts) is re-patched on the next start. Fails open:
if an anchor is missing (upstream changed the code), that file is left stock
and a warning is printed — the session still starts.
"""
import glob
import os
import sys

MARKER = "// local-patch: custom-emoji"

# Only the installed cache copy runs. Never the marketplace clone — it is a git
# checkout and a dirty tree would break `claude plugin marketplace update`.
ROOTS = [
    os.path.expanduser("~/.claude/plugins/cache/claude-plugins-official/telegram/*/server.ts"),
]

EDITS = [
    # 1. react tool schema: accept custom_emoji_id.
    (
        """      description: 'Add an emoji reaction to a Telegram message. Telegram only accepts a fixed whitelist (👍 👎 ❤ 🔥 👀 🎉 etc) — non-whitelisted emoji will be rejected.',
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: { type: 'string' },
          message_id: { type: 'string' },
          emoji: { type: 'string' },
        },
        required: ['chat_id', 'message_id', 'emoji'],
      },
    },
""",
        """      description: 'Add an emoji reaction to a Telegram message. Standard reactions: a fixed whitelist (👍 👎 ❤ 🔥 👀 🎉 etc) — non-whitelisted emoji will be rejected. Custom (Premium) reactions: pass custom_emoji_id (from the custom_emoji tool) — works only where the chat allows that custom reaction or it is already on the message; emoji is then ignored.',
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: { type: 'string' },
          message_id: { type: 'string' },
          emoji: { type: 'string' },
          custom_emoji_id: { type: 'string', description: 'Custom emoji id; sends a custom_emoji reaction instead of emoji.' },
        },
        required: ['chat_id', 'message_id', 'emoji'],
      },
    },
    { """ + MARKER + """
      name: 'custom_emoji',
      description: 'List custom-emoji reactions you can use. With chat_id: the custom reactions that chat allows (id, the standard emoji it stands for, pack name) — "all reactions allowed" chats have no list, pass a sticker_set instead. With sticker_set (pack short name, e.g. from a t.me/addemoji/<name> link): every emoji in that pack.',
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: { type: 'string' },
          sticker_set: { type: 'string' },
        },
      },
    },
""",
    ),
    # 2. react handler: custom_emoji reaction type.
    (
        """        await bot.api.setMessageReaction(args.chat_id as string, Number(args.message_id), [
          { type: 'emoji', emoji: args.emoji as ReactionTypeEmoji['emoji'] },
        ])
        return { content: [{ type: 'text', text: 'reacted' }] }
      }
""",
        """        const customId = args.custom_emoji_id as string | undefined """ + MARKER + """
        await bot.api.setMessageReaction(args.chat_id as string, Number(args.message_id), [
          customId
            ? { type: 'custom_emoji', custom_emoji_id: customId }
            : { type: 'emoji', emoji: args.emoji as ReactionTypeEmoji['emoji'] },
        ])
        return { content: [{ type: 'text', text: 'reacted' }] }
      }
      case 'custom_emoji': {
        const lines: string[] = []
        if (args.sticker_set) {
          const set = await bot.api.getStickerSet(args.sticker_set as string)
          for (const s of set.stickers) {
            if (s.custom_emoji_id) lines.push(`${s.custom_emoji_id} ${s.emoji ?? ''} ${set.name}`)
          }
        } else if (args.chat_id) {
          assertAllowedChat(args.chat_id as string)
          const chat = await bot.api.getChat(args.chat_id as string)
          const avail = (chat as { available_reactions?: Array<{ type: string; emoji?: string; custom_emoji_id?: string }> }).available_reactions
          if (!avail) {
            return { content: [{ type: 'text', text: 'all reactions allowed (no list) — pass sticker_set to get ids from a pack' }] }
          }
          const ids = avail.filter(r => r.type === 'custom_emoji').map(r => r.custom_emoji_id!)
          const std = avail.filter(r => r.type === 'emoji').map(r => r.emoji).join(' ')
          for (let i = 0; i < ids.length; i += 200) {
            const stickers = await bot.api.getCustomEmojiStickers(ids.slice(i, i + 200))
            for (const s of stickers) lines.push(`${s.custom_emoji_id} ${s.emoji ?? ''} ${s.set_name ?? ''}`)
          }
          if (std) lines.push(`standard allowed: ${std}`)
        } else {
          throw new Error('pass chat_id or sticker_set')
        }
        return { content: [{ type: 'text', text: lines.join('\\n') || 'no custom reactions allowed' }] }
      }
""",
    ),
]


def patch(path: str) -> str:
    src = open(path, encoding="utf-8").read()
    if MARKER in src:
        return "already patched"
    out = src
    for old, new in EDITS:
        if out.count(old) != 1:
            return "SKIPPED: anchor not found (upstream changed) — left stock"
        out = out.replace(old, new)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(out)
    os.replace(tmp, path)
    return "patched"


def main() -> int:
    for pattern in ROOTS:
        for path in sorted(glob.glob(pattern)):
            try:
                print(f"telegram-plugin patch: {path}: {patch(path)}")
            except OSError as e:
                print(f"telegram-plugin patch: {path}: SKIPPED: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
