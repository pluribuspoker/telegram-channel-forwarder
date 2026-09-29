#!/usr/bin/env python3
"""PreToolUse hook (repo-level, every clone): an FYI before a .env write, never a gate.

The VPS .env is the source of truth (2026-09-28). deploy/env_backup.py
reconciles every write to /home/forwarder/app/.env that did not come through
scripts/set_env_local.py back to its newest snapshot within seconds, and a
push (scp / the old syncenv) is reconciled the same way. The writer sees
success either way — only the operator gets the DM — so an agent would
believe a change landed that was reverted. Checked in under .claude/ so every
Claude Code session in any clone gets it: the VPS sessions, the operator's
desktop, and AK's receptionist agents (which reach prod via
`sudo -u forwarder`).

Advisory only (the operator rejected blocking hooks, 2026-09-28): it may attach
hookSpecificOutput.additionalContext naming the sanctioned command. Heuristic
by design — a miss costs nothing the server guard doesn't already catch.
Any error → silent. Runs under python3 or python (Windows).
"""
import json
import re
import sys

SANCTIONED = re.compile(r"set_env_local\.py|env_mappings\.py|pull_env\.py|env_backup\.py|test_env_guard")
# `.env` as a file name — not .env.local / .env.guesser / .env.<backup>, not foo.env
ENV_TOKEN = r"(?<![\w.-])(?:[\w./~-]*/)?\.env(?![\w.-])"
SERVER_ENV = re.compile(r"/home/forwarder/app/\.env(?![\w.-])")
PUSH = re.compile(
    r"\bsyncenv\b"
    r"|\b(?:scp|rsync)\b[^\n|;&]*" + ENV_TOKEN + r"[^\n|;&]*\S+:\S*"
)
WRITE = re.compile(
    r"\bsed\b[^\n|;&]*\s-i[^\n|;&]*" + ENV_TOKEN
    + r"|>>?\s*['\"]?" + ENV_TOKEN
    + r"|\btee\b[^\n|;&]*" + ENV_TOKEN
    + r"|\b(?:cp|mv|install)\b[^\n|;&]*\s" + ENV_TOKEN + r"['\"]?\s*(?:$|[;&|)\n])"
    + r"|open\([^)]*" + ENV_TOKEN + r"[^)]*['\"][wa]"
    + r"|write_text\(|write_bytes\(|dotenv\.set_key|set_key\("
)

FIX = ("Change a server value with `python3 scripts/set_env_local.py --file .env KEY=VALUE` "
       "(`--unset KEY`, `--stdin KEY`) or `scripts/env_mappings.py list|add|remove`, run in "
       "/home/forwarder/app on the VPS (as forwarder); refresh a desktop copy with "
       "`scripts/pull_env.py`.")


def classify(tool, inp):
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = str(inp.get("file_path") or inp.get("notebook_path") or "").replace("\\", "/")
        if SERVER_ENV.search(path):
            return "server"
        if re.search(r"(?:^|/)\.env$", path):
            return "local"
        return None
    if tool != "Bash":
        return None
    cmd = str(inp.get("command") or "")
    if SANCTIONED.search(cmd):
        return None
    if PUSH.search(cmd):
        return "push"
    if not re.search(ENV_TOKEN, cmd):
        return None
    if WRITE.search(cmd):
        on_server = SERVER_ENV.search(cmd) or re.search(r"forwarder/app|sudo\b.*-u\s*forwarder", cmd)
        return "server" if on_server else "maybe"
    return None


def note(kind: str) -> str:
    if kind == "push":
        return ("FYI (env guard): pushing .env to the VPS doesn't change it — the VPS .env is the "
                "source of truth and a guard reconciles any pushed copy back within seconds (only "
                "genuinely NEW keys survive; changed/removed values are reverted and DM'd to the "
                "operator, not to you). " + FIX)
    if kind == "server":
        return ("FYI (env guard): a direct write to the VPS .env gets reverted within seconds by "
                "the env guard (only set_env_local.py writes stick; changed/removed keys are rolled "
                "back and DM'd to the operator — you'd see success either way). " + FIX)
    if kind == "local":
        return ("FYI (env guard): this is a local copy of .env — editing it never changes production "
                "(the VPS .env is the source of truth and rejects pushed changes). Fine for local "
                "testing; for a prod change: " + FIX)
    return ("FYI (env guard): if this writes the VPS .env (/home/forwarder/app/.env), it will be "
            "reverted within seconds — only set_env_local.py writes stick. " + FIX)


def main() -> None:
    try:
        inp = json.load(sys.stdin)
        kind = classify(inp.get("tool_name", ""), inp.get("tool_input") or {})
        if kind:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "additionalContext": note(kind)}}))
    except Exception:
        pass


if __name__ == "__main__":
    main()
