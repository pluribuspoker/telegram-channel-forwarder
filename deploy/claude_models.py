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
import sys
from pathlib import Path

HOME_DIR = Path(os.environ.get("HOME") or "/home/forwarder")
CLAUDE_SETTINGS = HOME_DIR / ".claude" / "settings.json"
PANE = "claude"

# Aliases resolve to full ids rather than being passed through: `/model opus`
# has to mean the same model in six months as it does today, and an id is what
# ends up on the launcher's command line.
MODEL_CHOICES = {
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
}
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# The machine default (operator, 2026-10-11): every COMMANDED restart — watchdog
# /restart, chat self-restart — resets the state file to this unless the
# command's own arguments name a model/effort. /model and /effort still persist
# live switches, so a crash relaunch keeps them; only the next commanded restart
# snaps back. Bash twin in run_claude_channels.sh (the fallback for a missing
# state file); scripts/test_claude_model_switch.py pins the two in step.
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
# Must start alphanumeric: the value lands on a shell command line, where a
# leading dash would be read as a claude CLI flag rather than a model.
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[1m\])?$")


def resolve_model(arg: str) -> str | None:
    """Alias or full model id -> the id to hand the CLI. None if it is neither."""
    arg = arg.strip()
    return MODEL_CHOICES.get(arg.lower()) or (arg if MODEL_ID_RE.match(arg) else None)


def named(model_id: str) -> str:
    """`claude-opus-5-5` -> `claude-opus-5-5 (opus)` when we have an alias for it."""
    for alias, full in MODEL_CHOICES.items():
        if full == model_id:
            return f"{model_id} ({alias})"
    return model_id


# --- next-start state ------------------------------------------------------
# What run_claude_channels.sh reads at every service start. Lives OUTSIDE the
# repo so a `git pull`/syncenv can never move the session onto a model the
# account can't bill (the 2026-09-11 fourteen-hour lesson).
STATE_FILE = Path(os.environ.get("CLAUDE_CHANNELS_STATE") or HOME_DIR / ".claude-channels.env")


def read_state() -> dict[str, str]:
    out: dict[str, str] = {}
    if STATE_FILE.exists():
        for line in STATE_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("\"'")
    return out


def write_state(**values: str) -> None:
    """Update keys in STATE_FILE atomically — a restart may read it mid-write."""
    state = read_state()
    state.update(values)
    body = (
        "# Written by deploy/claude_models.py — watchdog bot /model, /effort,\n"
        "# /restart, and restart_and_ping.sh (`reset-state`).\n"
        "# Read by deploy/run_claude_channels.sh at every service start.\n"
        + "".join(f"{k}={v}\n" for k, v in sorted(state.items()))
    )
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(body)
    tmp.replace(STATE_FILE)


def classify_restart_args(tokens: list[str]) -> tuple[str, str]:
    """(model id, effort) a commanded restart should land on: the machine
    default, except the parts the arguments name — order-free, so `/restart
    fable max` and `/restart max fable` agree.

    Deliberately STRICTER than resolve_model: a full id must start with
    `claude-`. resolve_model's regex admits any bare word, and a typo'd token
    silently becoming `--model opsu` doesn't fall back — the CLI refuses it,
    the tmux session dies, and the service boot-loops. Refusing here keeps a
    fat-fingered /restart from becoming an outage.
    """
    model = effort = None
    for tok in tokens:
        t = tok.strip()
        if not t:
            continue
        low = t.lower()
        if low in EFFORT_LEVELS:
            if effort:
                raise ValueError(f"Two efforts given ({effort}, {low}) — pick one.")
            effort = low
        elif (resolved := resolve_model(t)) and (low in MODEL_CHOICES or low.startswith("claude-")):
            if model:
                raise ValueError(f"Two models given ({model}, {resolved}) — pick one.")
            model = resolved
        else:
            raise ValueError(
                f"Unknown arg '{t}'. Model: {' | '.join(MODEL_CHOICES)} or a full "
                f"claude-… id. Effort: {'|'.join(EFFORT_LEVELS)}."
            )
    return model or DEFAULT_MODEL, effort or DEFAULT_EFFORT


def _reset_state_cli(argv: list[str]) -> int:
    """`claude_models.py reset-state [model] [effort]` — the restart scripts'
    way to point the next start at the default (or the stated override)."""
    try:
        model, effort = classify_restart_args(argv)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    write_state(CLAUDE_CHANNELS_MODEL=model, CLAUDE_CHANNELS_EFFORT=effort)
    print(f"model={model}\neffort={effort}")
    return 0


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


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "reset-state":
        raise SystemExit(_reset_state_cli(sys.argv[2:]))
    print(f"usage: {sys.argv[0]} reset-state [model] [effort]", file=sys.stderr)
    raise SystemExit(2)
