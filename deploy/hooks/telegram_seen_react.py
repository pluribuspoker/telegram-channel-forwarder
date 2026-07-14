#!/usr/bin/env python3
"""UserPromptSubmit hook: react 👀 to every inbound Telegram message.

In the Telegram-channels setup, the user has no way to tell whether a message
they sent actually reached the session — the Bot API has no history/backfill, so
a message sent during a restart window (before the new process's poll loop is
connected) is silently dropped and looks identical to one that's simply not been
answered yet.

This hook fires at UserPromptSubmit — the instant the harness hands a submitted
message to the model — and drops a 👀 reaction on it via the Bot API. That gives
the user a hard delivery receipt: reaction present = the session received it;
reaction absent = it was dropped, resend. It runs at the harness level (not a
model tool call), so it's guaranteed on every message and never subject to the
model forgetting or a crash mid-turn.

Self-gating: only acts when the prompt carries a Telegram <channel> tag, so it's
a no-op for local/non-Telegram sessions. python3 only (no bare `python` on this
VPS). Never blocks; always exits 0.
"""
import json, os, sys, re, urllib.request, urllib.parse

ENV = os.path.expanduser("~/.claude/channels/telegram/.env")
EMOJI = "\U0001F440"  # 👀

def log(*a):  # best-effort debug, never fatal
    try:
        with open("/tmp/tg_seen_react.log", "a") as f:
            f.write(" ".join(str(x) for x in a) + "\n")
    except Exception:
        pass

def main():
    try:
        inp = json.load(sys.stdin)
    except Exception:
        return

    prompt = inp.get("prompt") or ""
    if not isinstance(prompt, str) or 'source="plugin:telegram:telegram"' not in prompt:
        return  # not a Telegram turn — self-gate

    # Extract chat_id + message_id from the (last) Telegram <channel ...> tag.
    # Attribute order isn't guaranteed, so match each independently within the tag.
    tags = re.findall(r'<channel\s[^>]*source="plugin:telegram:telegram"[^>]*>', prompt)
    if not tags:
        return
    tag = tags[-1]
    cm = re.search(r'chat_id="(-?\d+)"', tag)
    mm = re.search(r'message_id="(\d+)"', tag)
    if not cm or not mm:
        log("no chat/msg id in tag", tag[:200])
        return
    chat_id, message_id = cm.group(1), mm.group(1)

    if os.environ.get("TG_SEEN_DRYRUN") == "1":
        print(f"[DRYRUN] would react {EMOJI} on chat={chat_id} msg={message_id}")
        return

    token = _bot_token()
    if not token:
        log("no bot token")
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": json.dumps([{"type": "emoji", "emoji": EMOJI}]),
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/setMessageReaction", data=data)
        urllib.request.urlopen(req, timeout=15).read()
        log("reacted ok chat", chat_id, "msg", message_id)
    except Exception as e:
        log("react failed", e)

def _bot_token():
    try:
        for line in open(ENV):
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get("TELEGRAM_BOT_TOKEN")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("fatal", e)
    sys.exit(0)
