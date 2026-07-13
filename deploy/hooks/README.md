# Claude Code hooks

Version-controlled copies of the Claude Code hooks that run on the VPS. Live
copies live in `~/.claude/hooks/` (forwarder home); edit here, then sync.

## `post_investigate.sh`

A **Stop hook** that, after each `/investigate` task, blocks Claude from ending
its turn until it has (1) added any lessons to `.claude/commands/investigate.md`
and (2) saved relevant feedback to memory.

Key behaviors:
- **Fires once per investigation**, not once per session. It counts genuine
  `/investigate` invocations in the transcript (Skill-tool invocations +
  typed slash commands in user messages) and re-fires whenever that count
  exceeds the count stored in its flag file
  (`/tmp/claude_investigate_reminded_<transcript-hash>`). This is required
  because the Telegram tmux session never ends and `/clear` does not start a
  new transcript — a boolean flag would fire only once for the whole session.
- **Pollution-resistant**: counts by parsing the transcript as JSON, ignoring
  the `investigate` string when it appears inside Bash/Write tool inputs, tool
  results, or assistant prose.
- Uses `python3` (this VPS has **no bare `python`** — a bare `python` call here
  silently no-op'd the hook for a while).

## `telegram_delivery_guard.py`

A **Stop hook** that guarantees every Telegram turn actually delivers something.

In the Telegram-channels setup, plain model output is **not** sent to the user —
only an explicit `reply` (or `edit_message`) tool call reaches Telegram. If a
turn ends without one, the user sees nothing, which is indistinguishable from a
crash. (This bit us once: a "which one is best" answer was generated as prose but
never sent, so it looked like the bot died.)

At Stop, the guard:
- finds the last inbound `<channel source="plugin:telegram:telegram" …>` message
  and its `chat_id` (self-gating — exits silently on non-Telegram sessions);
- scans the turn after it for a `…__reply` / `…__edit_message` tool call;
- if none was made, **recovers the assistant text from that turn and sends it**
  via the Bot API, prefixed with an "⚠️ Auto-recovered…" marker — so the user
  gets the real answer, not just a warning. If there's no text either (genuine
  crash/empty turn), it sends a short "re-send your message" diagnostic instead.

Notes:
- Never blocks, always exits 0. Does not duplicate: if a `reply` was made, it
  stays silent.
- `python3` only. Bot token read from `~/.claude/channels/telegram/.env`.
- Dry-run test: `echo '{"transcript_path":"<jsonl>"}' | TG_GUARD_DRYRUN=1 python3 telegram_delivery_guard.py`
- Debug log: `/tmp/tg_delivery_guard.log`.

Sync: `cp deploy/hooks/telegram_delivery_guard.py ~/.claude/hooks/ && chmod +x ~/.claude/hooks/telegram_delivery_guard.py`

## Sync a changed hook to the VPS

```bash
cp deploy/hooks/post_investigate.sh ~/.claude/hooks/post_investigate.sh
chmod +x ~/.claude/hooks/post_investigate.sh
```

No restart needed — Claude Code reads the hook script fresh on each fire.

## Windows / Git Bash port

`post_investigate.win.sh` is the local-machine port for Windows + Git Bash.
Only difference from the Linux version: `python` instead of `python3` (Windows
has no working bare `python3` — the Store stub errors out). Everything else
(`md5sum`, `/tmp` flag files) works transparently via Git Bash.

Install locally:

```bash
cp deploy/hooks/post_investigate.win.sh ~/.claude/hooks/post_investigate.sh
chmod +x ~/.claude/hooks/post_investigate.sh
```

## Registration (in `~/.claude/settings.json`)

The hook is wired as a `Stop` hook. `settings.json` is not in this repo (it
holds other machine config), but the relevant block is:

```json
{
  "hooks": {
    "Stop": [
      { "matcher": "", "hooks": [
        { "type": "command", "command": "bash ~/.claude/hooks/post_investigate.sh" }
      ] }
    ]
  }
}
```
