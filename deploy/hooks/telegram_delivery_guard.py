#!/usr/bin/env python3
"""Stop hook: guarantee every Telegram turn actually delivers something.

In the Telegram-channels setup, plain model output is NOT sent to the user —
only an explicit `reply` (or `edit_message`) tool call reaches Telegram. If a
turn ends without one, the user sees nothing: indistinguishable from a crash.

This hook fires at Stop, inspects the current turn, and if I answered an inbound
Telegram message WITHOUT delivering via the send tools, it recovers the text I
generated and sends it to that chat via the Bot API — prefixed with a marker so
the user knows it was auto-recovered. If there is no text either (a genuine
empty/crashed turn), it sends a short diagnostic instead of leaving silence.

python3 only (this VPS has no bare `python`). Never blocks; always exits 0.
"""
import json, os, sys, re, urllib.request, urllib.parse

def log(*a):  # best-effort debug, never fatal
    try:
        with open("/tmp/tg_delivery_guard.log", "a") as f:
            f.write(" ".join(str(x) for x in a) + "\n")
    except Exception:
        pass

def main():
    try:
        inp = json.load(sys.stdin)
    except Exception:
        return
    transcript = inp.get("transcript_path", "")
    if not transcript or not os.path.exists(transcript):
        return

    try:
        lines = open(transcript).read().splitlines()
    except OSError:
        return

    msgs = []
    for line in lines:
        try:
            o = json.loads(line)
        except Exception:
            continue
        m = o.get("message") or {}
        if m.get("role") in ("user", "assistant"):
            msgs.append(m)

    # Find the last inbound Telegram message (a <channel ...telegram...> user tag)
    # and its chat_id. Everything after it is "this turn".
    last_inbound = -1
    chat_id = None
    for i, m in enumerate(msgs):
        if m.get("role") != "user":
            continue
        text = _user_text(m)
        if 'source="plugin:telegram:telegram"' in text:
            mo = re.search(r'chat_id="(\d+)"', text)
            if mo:
                last_inbound = i
                chat_id = mo.group(1)
    if last_inbound == -1 or not chat_id:
        return  # not a Telegram turn — self-gating for local/other sessions

    turn = msgs[last_inbound + 1:]

    delivered = False
    texts = []
    for m in turn:
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                name = b.get("name", "")
                if name.endswith("__reply") or name.endswith("__edit_message"):
                    delivered = True
            elif b.get("type") == "text":
                t = (b.get("text") or "").strip()
                if t:
                    texts.append(t)

    if delivered:
        return  # normal path — user already got a reply

    # Undelivered turn. Recover the text if any, else emit a diagnostic.
    if texts:
        body = "\n\n".join(texts)
        prefix = ("⚠️ Auto-recovered — I generated this reply but didn't send it "
                  "through the delivery tool. Here it is:\n\n")
        out = prefix + body
    else:
        out = ("⚠️ My last turn finished without sending a reply and produced no "
               "recoverable text — likely a crash or a tool error mid-turn. "
               "Please re-send your message.")

    if len(out) > 4000:
        out = out[:3980] + "\n\n… (truncated)"

    if os.environ.get("TG_GUARD_DRYRUN") == "1":
        print(f"[DRYRUN] would send to {chat_id} ({len(out)} chars):\n{out}")
        return

    token = _bot_token()
    if not token:
        log("no bot token")
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": out,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=15).read()
        log("recovered send ok chat", chat_id, "len", len(out))
    except Exception as e:
        log("send failed", e)

def _user_text(m):
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
        return "\n".join(parts)
    return ""

def _bot_token():
    env = os.path.expanduser("~/.claude/channels/telegram/.env")
    try:
        for line in open(env):
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
