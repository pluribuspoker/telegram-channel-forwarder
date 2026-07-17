#!/bin/bash
# Stop hook: after each /investigate task, block Claude from stopping until it
# adds lessons + saves memory. Uses "decision":"block" so Claude actually sees
# the reason and gets a turn to act on it (systemMessage is user-only and
# invisible to Claude).
#
# Fires ONCE PER INVESTIGATION (not once per session). It counts genuine
# /investigate invocations in the transcript and re-fires whenever that count
# exceeds the count stored at the last fire. This matters because the Telegram
# tmux session never ends and /clear does not start a new transcript — a boolean
# "already reminded" flag fired only once for the entire tmux lifetime.
#
# NOTE: uses python3 (this VPS has no bare `python` — a bare `python` here
# silently failed and made this hook a no-op for a while).

INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null)
[ -z "$TRANSCRIPT" ] && exit 0

# Count GENUINE invocations: Skill tool_use (skill=investigate) in assistant
# messages, plus <command-name>/investigate</command-name> in USER-role text
# (typed slash command). Deliberately ignores the same string appearing inside
# Bash/Write tool inputs, tool results, and assistant prose — those are where
# transcript "pollution" lives and would inflate a naive grep.
COUNT=$(TRANSCRIPT="$TRANSCRIPT" python3 <<'PYEOF'
import os, json
try:
    lines = open(os.environ["TRANSCRIPT"]).read().splitlines()
except OSError:
    lines = []
n = 0
TAG = "<command-name>/investigate</command-name>"
for line in lines:
    try:
        o = json.loads(line)
    except Exception:
        continue
    msg = o.get("message", {}) or {}
    role = msg.get("role")
    content = msg.get("content")
    if role == "user":
        if isinstance(content, str):
            if TAG in content:
                n += 1
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and TAG in (b.get("text") or ""):
                    n += 1
    elif role == "assistant" and isinstance(content, list):
        for b in content:
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("name") == "Skill"
                    and (b.get("input") or {}).get("skill") == "investigate"):
                n += 1
print(n)
PYEOF
)

case "$COUNT" in ''|*[!0-9]*) exit 0;; esac
[ "$COUNT" -eq 0 ] && exit 0

FLAG="/tmp/claude_investigate_reminded_$(echo "$TRANSCRIPT" | md5sum | cut -d' ' -f1)"
LAST=0
[ -f "$FLAG" ] && LAST=$(cat "$FLAG" 2>/dev/null)
case "$LAST" in ''|*[!0-9]*) LAST=0;; esac

# Re-fire only when a NEW investigation has appeared since the last reminder.
if [ "$COUNT" -gt "$LAST" ]; then
  echo "$COUNT" > "$FLAG"
  printf '{"decision":"block","reason":"POST-INVESTIGATION REVIEW: You just completed an /investigate task. (1) Review whether you made any mistakes that represent GENERALIZABLE lessons — operational traps, debugging principles, or constraints that would apply to unrelated future investigations. If yes, add to .claude/commands/investigate.md Lessons. If the fix was code-only or too incident-specific, skip — no lesson needed. Do NOT add a lesson just to have one. (2) Save relevant feedback to memory if appropriate."}'
fi
