#!/usr/bin/env python3
"""Idempotently patch the Telegram channel plugin for custom-emoji reactions.

The stock plugin hardcodes `{type: 'emoji'}` in its react tool, so the session
can only use Telegram's ~70 standard reactions. This adds:

  * react: optional `custom_emoji_id` → routed through the warm reaction
    helper (deploy/react_helper.py, claude-react-helper.service: operator
    session seeds the emoji, bot copies it, session unseeds), with a direct Bot
    API attempt as the fallback (works where the emoji is already on the
    message or a channel whitelists it)
  * inbound message dates remembered per chat:message — the helper maps the
    bot-side id to the operator's side by date in DMs/basic groups
  * custom_emoji tool: list the custom reactions a chat allows, or every emoji
    in a custom-emoji pack

Run by deploy/run_claude_channels.sh before every launch, so a plugin update
(new cache dir, stock server.ts) is re-patched on the next start. The pristine
file is kept as server.ts.stock; an older patch version is re-applied from it.
Fails open: if an anchor is missing (upstream changed the code), that file is
left stock and a warning is printed — the session still starts.
"""
import glob
import os
import sys

MARKER = "// local-patch: custom-emoji v2"
ANY_MARKER = "// local-patch: custom-emoji"

# Only the installed cache copy runs; the marketplace copy is left pristine.
ROOTS = [
    os.path.expanduser("~/.claude/plugins/cache/claude-plugins-official/telegram/*/server.ts"),
]

HELPERS = """// local-patch: custom-emoji v2 — warm seed/copy/unseed helper
// (deploy/react_helper.py). Private chats and basic groups number messages per
// account, so the helper maps our message id to the operator's by date.
const REACT_HELPER_SOCK = '/run/claude-react/react.sock'
const inboundMeta = new Map<string, { date: number; text: string | null }>()

function rememberInbound(chat_id: string, msgId: number, date: number | undefined, text: string | null): void {
  if (date == null) return
  inboundMeta.set(`${chat_id}:${msgId}`, { date, text })
  if (inboundMeta.size > 500) inboundMeta.delete(inboundMeta.keys().next().value!)
}

function callReactHelper(req: Record<string, unknown>, timeoutMs = 8000): Promise<{ ok: boolean; error?: string; ms?: Record<string, number> }> {
  return new Promise((resolve, reject) => {
    const sock = createConnection(REACT_HELPER_SOCK)
    let buf = ''
    const timer = setTimeout(() => { sock.destroy(); reject(new Error('react helper timeout')) }, timeoutMs)
    sock.on('connect', () => sock.write(JSON.stringify(req) + '\\n'))
    sock.on('data', d => {
      buf += d.toString()
      const nl = buf.indexOf('\\n')
      if (nl < 0) return
      clearTimeout(timer)
      sock.end()
      try { resolve(JSON.parse(buf.slice(0, nl))) } catch (e) { reject(e) }
    })
    sock.on('error', e => { clearTimeout(timer); reject(e) })
  })
}

"""

EDITS = [
    # 0. node:net for the helper's unix socket.
    (
        "import { join, extname, sep } from 'path'\n",
        "import { join, extname, sep } from 'path'\nimport { createConnection } from 'net' " + MARKER + "\n",
    ),
    # 1. react tool schema: accept custom_emoji_id; custom_emoji tool.
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
        """      description: 'Add an emoji reaction to a Telegram message. Standard reactions: a fixed whitelist (👍 👎 ❤ 🔥 👀 🎉 etc) — non-whitelisted emoji will be rejected. Custom (Premium) reactions: pass custom_emoji_id (ids from the custom_emoji tool) — works in the operator\\'s chats (DM included) via the reaction helper, ~0.3 s; emoji is then ignored.',
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
    # 2. react handler: helper first for custom emoji, direct as the fallback.
    (
        """        await bot.api.setMessageReaction(args.chat_id as string, Number(args.message_id), [
          { type: 'emoji', emoji: args.emoji as ReactionTypeEmoji['emoji'] },
        ])
        return { content: [{ type: 'text', text: 'reacted' }] }
      }
""",
        """        const customId = args.custom_emoji_id as string | undefined """ + MARKER + """
        if (customId) {
          const meta = inboundMeta.get(`${args.chat_id}:${args.message_id}`)
          let helperErr = ''
          try {
            const r = await callReactHelper({
              op: 'react',
              chat_id: Number(args.chat_id),
              message_id: Number(args.message_id),
              custom_emoji_id: customId,
              date: meta?.date ?? null,
              text: meta?.text ?? null,
            })
            if (r.ok) return { content: [{ type: 'text', text: `reacted (custom, ${r.ms?.total ?? '?'} ms)` }] }
            helperErr = r.error ?? 'unknown error'
          } catch (e) {
            helperErr = e instanceof Error ? e.message : String(e)
          }
          try {
            await bot.api.setMessageReaction(args.chat_id as string, Number(args.message_id), [
              { type: 'custom_emoji', custom_emoji_id: customId },
            ])
            return { content: [{ type: 'text', text: 'reacted (custom, direct)' }] }
          } catch (e) {
            throw new Error(`custom reaction failed — helper: ${helperErr}; direct: ${e instanceof Error ? e.message : String(e)}`)
          }
        }
        await bot.api.setMessageReaction(args.chat_id as string, Number(args.message_id), [
          { type: 'emoji', emoji: args.emoji as ReactionTypeEmoji['emoji'] },
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
    # 3. helper client + inbound map, top level before handleInbound.
    (
        "async function handleInbound(\n",
        HELPERS + "async function handleInbound(\n",
    ),
    # 4. remember each inbound message's date (and text as a tiebreak).
    (
        "  const msgId = ctx.message?.message_id\n",
        "  const msgId = ctx.message?.message_id\n"
        "  if (msgId != null) rememberInbound(chat_id, msgId, ctx.message?.date, ctx.message?.text ?? ctx.message?.caption ?? null) "
        + MARKER + "\n",
    ),
]


def patch(path: str) -> str:
    src = open(path, encoding="utf-8").read()
    if MARKER in src:
        return "already patched"
    stock = path + ".stock"
    if ANY_MARKER in src:
        # An older patch version: start again from the pristine copy.
        if not os.path.exists(stock):
            return "SKIPPED: older patch present and no server.ts.stock — left as is"
        src = open(stock, encoding="utf-8").read()
    else:
        with open(stock, "w", encoding="utf-8") as f:
            f.write(src)
    out = src
    for old, new in EDITS:
        if out.count(old) != 1:
            return "SKIPPED: anchor not found (upstream changed) — left as is"
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
