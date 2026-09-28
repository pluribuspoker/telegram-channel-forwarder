#!/usr/bin/env python3
"""PreToolUse hook on the Telegram `reply` tool: enforce the agreed chat style.

The operator approved (2026-09-26) one live-edited status message per task plus
a NEW message only at the end (edits don't ping the phone), and expressive emoji
reactions. Memory alone didn't hold: on 2026-09-27 a long /investigate sent a
dozen separate "on it" replies — one per "user hasn't heard from you" nudge —
and zero reactions beyond the hook's 👀. The operator asked for a mechanism, not
a promise. Two checks, each over the CURRENT turn (everything after the last
inbound Telegram message):

1. No reaction yet → the FIRST reply is denied once: react to the inbound
   message first.
2. A reply already went out this turn → every further reply is denied once:
   progress belongs in edit_message on that message.

Deny-once, not deny-always: re-sending the identical call passes. That keeps
the legitimate cases (the final result, a question that needs an answer, a
message that genuinely wants no reaction) one deliberate retry away, while the
habit — firing off a new message by reflex — always hits a wall first.

Self-gating: only acts in turns answering a Telegram channel message. Never
crashes the call: any internal error allows. python3 only (no bare `python`).
Debug log: /tmp/tg_style_guard.log. State: /tmp/tg_style_guard.json.
"""
import hashlib
import json
import os
import re
import sys
import time

STATE = os.environ.get("TG_STYLE_STATE", "/tmp/tg_style_guard.json")
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


def turn_state(transcript, current_id):
    """(inbound message_id, prior reply ids, reacted?, turn key) for the current turn."""
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
    # Only calls that actually went out count: a reply this guard denied (or
    # that errored) has a tool_result without "sent (id: N)" — counting it made
    # the guard report "already replied 2x" after one real send and one denial.
    results = {}
    for m in msgs[last + 1:]:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = json.dumps(b.get("content"))
    replies, reacted = [], False
    for m in msgs[last + 1:]:
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if not isinstance(b, dict) or b.get("type") != "tool_use" or b.get("id") == current_id:
                continue
            name, res = b.get("name", ""), results.get(b.get("id"), "")
            if name.endswith("__reply") and "sent (id:" in res:
                replies.append(re.search(r"sent \(id: (\d+)\)", res).group(1))
            elif name.endswith("__react") and (not res or "reacted" in res):
                # No result yet = in flight: a react issued in parallel with this
                # reply (same assistant message) has no tool_result at PreToolUse
                # time — requiring one blocked a correctly-reacted reply.
                reacted = True
    return inbound, replies, reacted, f"{transcript}:{last}:{inbound}"


def main():
    inp = json.load(sys.stdin)
    if not (inp.get("tool_name") or "").endswith("__reply"):
        return None
    transcript = inp.get("transcript_path") or ""
    if not os.path.exists(transcript):
        return None
    st = turn_state(transcript, inp.get("tool_use_id"))
    if not st:
        return None  # not a Telegram turn
    inbound, replies, reacted, key = st
    tool_input = inp.get("tool_input") or {}
    digest = hashlib.sha256(json.dumps(tool_input, sort_keys=True).encode()).hexdigest()[:16]

    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    denied = state.get(key, [])
    if digest in denied:
        log("allow retry", key, digest)
        return None  # deliberate re-send of a denied call

    reason = None
    if not replies and not reacted:
        reason = (f"Style guard: no reaction yet on the operator's message {inbound}. "
                  "React first (expressive — the operator enjoys them; never ⚡/👍 unless it's a "
                  "restart request), then send this reply. If this message truly wants no "
                  "reaction, re-send the identical reply call and it will pass.")
    elif replies:
        target = replies[0]  # the turn's first sent reply = its status message
        reason = (f"Style guard: you already replied this turn ({len(replies)}x). Progress "
                  f"updates go in edit_message on message {target} — edits don't ping, which is "
                  "the point. A NEW reply is only for the final result or a question that needs "
                  "an answer; if that's what this is, re-send the identical reply call and it "
                  "will pass.")
    if not reason:
        return None

    denied.append(digest)
    state[key] = denied[-20:]
    if len(state) > 200:
        state = dict(list(state.items())[-100:])
    try:
        tmp = STATE + ".tmp"
        json.dump(state, open(tmp, "w"))
        os.replace(tmp, STATE)
    except Exception as e:
        log("state write failed", e)
        return None  # can't remember the denial → a retry would loop; allow instead
    log("deny", key, digest, reason[:60])
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


if __name__ == "__main__":
    try:
        out = main()
        if out:
            print(json.dumps(out))
    except Exception as e:
        log("error", repr(e))
    sys.exit(0)
