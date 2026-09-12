#!/usr/bin/env python3
"""The model table the watchdog bot and the limit watchdog share.

One table, because two would drift: the bot offers a model the watchdog never
probes, or the watchdog names a fix the bot can't actually perform. The bash
half of the same contract lives in run_claude_channels.sh (`MODEL_ID_RE` has a
twin there); `scripts/test_claude_model_switch.py` pins them in step.
"""
import json
import os
import re
import subprocess
from pathlib import Path

HOME_DIR = Path(os.environ.get("HOME") or "/home/forwarder")
CLAUDE_SETTINGS = HOME_DIR / ".claude" / "settings.json"
PANE = "claude"

# Aliases resolve to full ids rather than being passed through: `/model opus`
# has to mean the same model in six months as it does today, and an id is what
# ends up on the launcher's command line.
MODEL_CHOICES = {
    "opus": "claude-opus-5",
    "fable": "claude-fable-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
}
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# Must start alphanumeric: the value lands on a shell command line, where a
# leading dash would be read as a claude CLI flag rather than a model.
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[1m\])?$")


def resolve_model(arg: str) -> str | None:
    """Alias or full model id -> the id to hand the CLI. None if it is neither."""
    arg = arg.strip()
    return MODEL_CHOICES.get(arg.lower()) or (arg if MODEL_ID_RE.match(arg) else None)


def named(model_id: str) -> str:
    """`claude-opus-5` -> `claude-opus-5 (opus)` when we have an alias for it."""
    for alias, full in MODEL_CHOICES.items():
        if full == model_id:
            return f"{model_id} ({alias})"
    return model_id


def _run(argv: list[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def settings_model() -> str:
    """The id /model last saved. The CLI writes it only for a model it accepted."""
    try:
        return json.loads(CLAUDE_SETTINGS.read_text()).get("model", "")
    except (OSError, ValueError):
        return ""


def claude_process() -> tuple[int | None, str]:
    """(pid, cmdline) of the session itself — never the tmux that spawned it.

    `pgrep -f "claude --channels"` matches the tmux new-session line too, and
    that one's start time is not the session's.
    """
    for line in _run(["pgrep", "-af", "claude --channels"]).splitlines():
        pid, _, cmd = line.partition(" ")
        if cmd and Path(cmd.split()[0]).name == "claude" and pid.isdigit():
            return int(pid), cmd
    return None, ""


def launched_model(cmdline: str | None = None) -> str:
    if cmdline is None:
        _, cmdline = claude_process()
    m = re.search(r"--model (\S+)", cmdline or "")
    return m.group(1) if m else ""


def _process_start(pid: int) -> float | None:
    """Epoch seconds the process started, via elapsed time (no /proc parsing)."""
    import time
    out = _run(["ps", "-o", "etimes=", "-p", str(pid)]).strip()
    return time.time() - int(out) if out.isdigit() else None


def session_model() -> tuple[str, str]:
    """(model id, how we know) for the model the LIVE session is on.

    The launch flag and the saved default disagree the moment anyone runs
    /model, which is now the normal case. The saved default wins only when the
    CLI wrote it *after* the process started — writing it is what /model does,
    so a later mtime is the evidence that a switch happened in this session.
    """
    pid, cmdline = claude_process()
    if pid is None:
        return "", "no session running"
    launched = launched_model(cmdline)
    saved = settings_model()
    if saved and saved != launched:
        started = _process_start(pid)
        try:
            mtime = CLAUDE_SETTINGS.stat().st_mtime
        except OSError:
            mtime = None
        if started and mtime and mtime > started:
            return saved, "switched in-session with /model"
    return launched, "launch flag"
