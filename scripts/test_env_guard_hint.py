"""Regression test: the repo-level env guard hint (.claude/hooks/env_guard_hint.py).

Offline, no side effects:

    ~/venv/bin/python scripts/test_env_guard_hint.py

The VPS .env guard (deploy/env_backup.py) reverts unsanctioned writes silently
from the writer's side, so every Claude Code session in any clone (VPS,
desktop, AK's receptionist) gets an advisory PreToolUse note first. Pins which
commands draw which note — and that the sanctioned tools and the other env
files draw none.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".claude" / "hooks"))

import env_guard_hint as h  # noqa: E402

BASH = [
    ("syncenv && git push", "push"),
    ("scp .env root@209.38.51.86:/home/forwarder/app/.env", "push"),
    ("rsync -av ./.env root@209.38.51.86:/home/forwarder/app/", "push"),
    ("sed -i 's/A=1/A=2/' /home/forwarder/app/.env", "server"),
    ("cd /home/forwarder/app && echo X=1 >> .env", "server"),
    ("sudo -n -u forwarder -H bash -lc 'cd /home/forwarder/app && sed -i s/a/b/ .env'", "server"),
    ("cp /tmp/new.env /home/forwarder/app/.env", "server"),
    ("python3 -c \"open('.env','w').write(x)\"", "maybe"),
    ("echo X=1 >> .env", "maybe"),
    # never flagged
    ("python3 scripts/set_env_local.py --file .env A=2", None),
    ("python3 scripts/env_mappings.py remove dfav-to-df", None),
    ("python scripts/pull_env.py", None),
    ("python3 scripts/set_env_local.py TELEGRAM_SESSION=abc", None),
    ("echo X=1 >> .env.local", None),
    ("cat .env | grep MAPPINGS", None),
    ("cp -p .env /tmp/env.before", None),
    ("grep -n FOO .env.guesser", None),
    ("scp root@209.38.51.86:/home/forwarder/app/.env /tmp/x", None),
]
FILES = [
    ("Edit", "/home/forwarder/app/.env", "server"),
    ("Write", "C:\\Users\\op\\telegram-forwarder\\.env", "local"),
    ("Edit", "/home/forwarder/app/.env.local", None),
    ("Edit", "/home/forwarder/app/listener.py", None),
]


def main():
    bad = []
    for cmd, want in BASH:
        got = h.classify("Bash", {"command": cmd})
        if got != want:
            bad.append(f"Bash {cmd!r}: want {want}, got {got}")
    for tool, path, want in FILES:
        got = h.classify(tool, {"file_path": path})
        if got != want:
            bad.append(f"{tool} {path!r}: want {want}, got {got}")
    assert not bad, "\n".join(bad)
    print(f"PASS classify: {len(BASH)} commands, {len(FILES)} file edits")

    # end to end: the hook's stdout is advisory context, never a decision
    out = subprocess.run(
        [sys.executable, str(ROOT / ".claude" / "hooks" / "env_guard_hint.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "syncenv"}}),
        capture_output=True, text=True, check=True).stdout
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and "permissionDecision" not in hso
    assert "set_env_local.py" in hso["additionalContext"]
    silent = subprocess.run(
        [sys.executable, str(ROOT / ".claude" / "hooks" / "env_guard_hint.py")],
        input="not json", capture_output=True, text=True).stdout
    assert silent == ""
    print("PASS hook I/O: advisory context only; garbage input → silent")


if __name__ == "__main__":
    main()
