#!/usr/bin/env python3
"""PostToolUse hook on the Telegram `reply` tool: an FYI about chat style, never a gate.

The operator approved (2026-09-26) one live-edited status message per long task,
a NEW message at the end (edits don't ping), and expressive emoji reactions. On
2026-09-27 a long /investigate drifted anyway: a dozen separate "on it" replies,
one per "user hasn't heard from you" nudge, and no reactions. A first version of
this hook DENIED such replies; the operator rejected that the next day — "I want
you to have control and do what you think is best, not be held down by rigid
rules" — so it only observes now. After a reply is sent, it may attach a short
note the model sees next (hookSpecificOutput.additionalContext — verified live:
PreToolUse/PostToolUse additionalContext both reach the model):

- first reply of the turn and no reaction on the operator's message yet;
- the third-or-later NEW message this turn (the second is usually the final),
  naming the status message an edit could go to.

Facts plus "your call" — no instructions, no blocking. Self-gating (Telegram
turns only); any error → silent. python3 only. Log: /tmp/tg_style_guard.log.
"""
import json
import os
import re
import sys
import time

LOG = "/tmp/tg_style_guard.log"
TG = 'source="plugin:telegram:telegram"'


def log(*a):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%F %T ") + " ".join(str(x) for x in a) + "\n")
    except Exception:
        pass


def _user_text(m):
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text") or "" for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def turn_state(transcript):
    """(inbound message_id, sent reply ids, reacted?) for the current Telegram turn."""
    msgs = []
    for line in open(transcript).read().splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        m = o.get("message") or {}
        if m.get("role") in ("user", "assistant"):
            msgs.append(m)
    last, inbound = -1, None
    for i, m in enumerate(msgs):
        if m.get("role") == "user":
            tags = re.findall(r"<channel\s[^>]*" + re.escape(TG) + r"[^>]*>", _user_text(m))
            if tags:
                mm = re.search(r'message_id="(\d+)"', tags[-1])
                last, inbound = i, (mm.group(1) if mm else None)
    if last == -1:
        return None
    sent, reacted = [], False
    for m in msgs[last + 1:]:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            if any(isinstance(b, dict) and b.get("type") == "tool_use"
                   and (b.get("name") or "").endswith("__react") for b in m["content"]):
                reacted = True
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    mo = re.search(r"sent \(id: (\d+)\)", json.dumps(b.get("content")))
                    if mo:
                        sent.append(mo.group(1))
    return inbound, sent, reacted


def main():
    inp = json.load(sys.stdin)
    if not (inp.get("tool_name") or "").endswith("__reply"):
        return None
    transcript = inp.get("transcript_path") or ""
    if not os.path.exists(transcript):
        return None
    st = turn_state(transcript)
    if not st:
        return None
    inbound, sent, reacted = st
    mo = re.search(r"sent \(id: (\d+)\)", json.dumps(inp.get("tool_response")))
    if mo and mo.group(1) not in sent:
        sent.append(mo.group(1))  # this reply's own result may not be in the transcript yet
    note = None
    if len(sent) <= 1 and not reacted:
        note = (f"FYI (style hook): no reaction on the operator's message {inbound} yet. "
                "They enjoy reactions; add one if it fits. Your call.")
    elif len(sent) >= 3:
        note = (f"FYI (style hook): that's new message #{len(sent)} this turn; the first "
                f"({sent[0]}) could take progress as edits, which don't ping. Your call.")
    if not note:
        return None
    log("note", inbound, len(sent), reacted)
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}


if __name__ == "__main__":
    try:
        out = main()
        if out:
            print(json.dumps(out))
    except Exception as e:
        log("error", repr(e))
    sys.exit(0)
