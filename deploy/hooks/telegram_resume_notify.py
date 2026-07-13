#!/usr/bin/env python3
"""SessionStart hook (VPS only): DM the resume command for the PREVIOUS session.

Every restart of the Telegram-channels claude session starts a fresh context
(new UUID) — the prior conversation is only recoverable via its transcript. A
planned restart lets the model post a resume pointer as its last message, but a
crash/external restart has no last message. This hook closes that gap: on each
new session it finds the previous session's transcript and DMs the user the
one-line prompt that reloads it — so resume works after ANY restart.

Gated to the VPS (hostname `pickbot`) per user request. Skips resume/clear/
compact starts (only fires on a genuine fresh startup). python3 only; never
fails the session; always exits 0.
"""
import json, os, sys, glob, urllib.request, urllib.parse

PROJECT_DIR = os.path.expanduser("~/.claude/projects/-home-forwarder-app")
ACCESS = os.path.expanduser("~/.claude/channels/telegram/access.json")
ENV = os.path.expanduser("~/.claude/channels/telegram/.env")
ONLY_HOST = "pickbot"

def log(*a):
    try:
        with open("/tmp/tg_resume_notify.log", "a") as f:
            f.write(" ".join(str(x) for x in a) + "\n")
    except Exception:
        pass

def main():
    if os.uname().nodename != ONLY_HOST:
        return  # VPS only

    try:
        inp = json.load(sys.stdin)
    except Exception:
        inp = {}

    # Only fire on a genuine fresh start, not resume/clear/compact.
    source = (inp.get("source") or "").lower()
    if source in ("resume", "clear", "compact"):
        log("skip source", source)
        return

    cur_id = inp.get("session_id") or ""
    cur_tp = inp.get("transcript_path") or ""
    if not cur_id and cur_tp:
        cur_id = os.path.splitext(os.path.basename(cur_tp))[0]

    # Previous session = most-recently-modified transcript that isn't this one.
    files = glob.glob(os.path.join(PROJECT_DIR, "*.jsonl"))
    files = [f for f in files if os.path.splitext(os.path.basename(f))[0] != cur_id]
    if not files:
        log("no prior transcript")
        return
    prev = max(files, key=lambda f: os.path.getmtime(f))
    prev_id = os.path.splitext(os.path.basename(prev))[0]

    chat_id = _chat_id()
    if not chat_id:
        log("no chat_id")
        return
    token = _bot_token()
    if not token:
        log("no token")
        return

    msg = (
        "🔄 Channels session restarted.\n\n"
        "To resume the previous conversation, paste this back to me:\n\n"
        f"Resume previous session — Read {prev} , "
        "summarize where we left off, then continue."
    )
    if os.environ.get("TG_RESUME_DRYRUN") == "1":
        print(f"[DRYRUN] chat={chat_id} prev={prev_id}\n{msg}")
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": msg,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=15).read()
        log("sent resume notify chat", chat_id, "prev", prev_id)
    except Exception as e:
        log("send failed", e)

def _chat_id():
    try:
        d = json.load(open(ACCESS))
        allow = d.get("allowFrom") or []
        if allow:
            return str(allow[0])
    except Exception:
        pass
    return "5911202683"

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
