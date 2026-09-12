#!/usr/bin/env python3
"""Claude limit watchdog — DMs the operator when the model the VPS session runs on stops serving.

Why this exists: 2026-09-11. The account ran out of Fable credits at 07:52.
Every Telegram message came back "You've reached your Fable limit. Run
/usage-credits to continue or switch models with /model" — including the ones
asking Claude to fix it, because the CLI answers that before the model ever sees
the prompt. The process was healthy, systemd reported the unit `active`, the
credentials were fine, and nothing said a word. It was noticed fourteen hours
later, by hand. Same shape as the 2026-08-11 credential expiry, and the same
lesson: the failures that cost days are the ones with no signal, not the ones
that are hard to fix. The fix here is one tap (`/model opus` in the watchdog
bot); the fourteen hours were entirely detection.

The auth watchdog cannot cover this. It probes Haiku — deliberately, because a
cheap model answers the question it asks — and it treats every 429 as a blip,
which is right for "are the credentials dead" and wrong for this. This probes
**the model the session is actually on** and treats a persistent refusal as the
outage it is.

Like the auth watchdog it probes `api.anthropic.com` directly and must never
shell out to `claude`: a second CLI process churns the channels plugin's bun
poller, which trips run_claude_channels.sh's "plugin gone" check and restarts
the very session being monitored.

Alerts on a state CHANGE only, with a checkmark when the model serves again —
the odds quota watchdog's rule. An unchanged condition is not news, and an alert
that repeats itself gets muted, which is how the real one goes unseen too.

Usage:
    python deploy/claude_limit_watchdog.py            # silent unless blocked
    python deploy/claude_limit_watchdog.py --report   # print the result, send nothing
    python deploy/claude_limit_watchdog.py --force    # send even if unchanged
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from claude_auth_watchdog import CLAUDE_CODE_SYSTEM, load_env, read_token, send
from claude_models import MODEL_CHOICES, named, session_model

STATE = Path.home() / ".claude_limit_watchdog_state.json"
# Off by default, like the odds quota watchdog: an unchanged condition is not
# news, and the recovery message already answers "is it back yet?".
REMIND_SECS = int(os.environ.get("CLAUDE_LIMIT_REMIND_SECS", 0))
# One 429 can be a burst. A refusal that survives this gap is the real thing.
CONFIRM_DELAY_SECS = int(os.environ.get("CLAUDE_LIMIT_CONFIRM_DELAY", 20))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (OSError, ValueError):
            pass
    return {}


def save_state(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE)


def classify(status: int, message: str) -> tuple[str, str]:
    """HTTP result -> one of ok / blocked / auth / unknown, plus a detail line.

    Only a refusal counts as blocked. 401/403 belongs to the auth watchdog and
    is deliberately silent here: two alarms for one fault is how both get muted.
    A 5xx, an overload or a network error is not an outage of the account, for
    the same reason an ESPN outage must never read as a bad parse.
    """
    if status == 200:
        return "ok", "serving"
    if status == 429:
        return "blocked", message or "refused (429), no message"
    if status in (401, 403):
        return "auth", f"HTTP {status} - {message or 'credentials rejected'} (the auth watchdog's alarm)"
    return "unknown", f"HTTP {status} - {message or 'no message'}"


def probe(token: str, model: str) -> tuple[str, str]:
    """One 1-token call, identifying as Claude Code. Never raises."""
    body = json.dumps({
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
        "system": CLAUDE_CODE_SYSTEM,
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return classify(r.status, "")
    except urllib.error.HTTPError as e:
        message = ""
        try:
            message = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        return classify(e.code, message)
    except Exception as e:  # noqa: BLE001
        return "unknown", f"could not reach the API ({e})"


def alternatives(token: str, blocked: str) -> list[str]:
    """Which of the other offered models answer right now — the alert's whole point.

    Naming a model that is also out would send the operator round the loop a
    second time, from a phone, in the middle of the outage.
    """
    usable = []
    for alias, model in MODEL_CHOICES.items():
        if model == blocked:
            continue
        if probe(token, model)[0] == "ok":
            usable.append(alias)
    return usable


def compose(condition: str, model: str, detail: str, usable: list[str]) -> str:
    if condition == "ok":
        return f"✅ Claude on the VPS is serving again — {named(model)}."
    fix = (
        f"Available right now: {', '.join(usable)}\nSwitch from here: /model {usable[0]}"
        if usable else
        "No other model answered either - this looks account-wide, not model-specific."
    )
    return (
        "\U0001f6ab Claude on the VPS is blocked.\n\n"
        f"{named(model)} is refusing requests:\n{detail}\n\n"
        "Every Telegram message to the session comes back with that, including "
        "the ones asking Claude to fix it - the CLI answers before the model "
        "ever sees the prompt.\n\n"
        f"{fix}"
    )


def decide(condition: str, model: str, state: dict, force: bool, now: float) -> tuple[bool, dict]:
    """(should_alert, next_state). Pure: the alerting rule, testable without a network.

    auth/unknown never write state. A network blip between two blocked probes
    must not read as a change and re-alert, and must not clear a real block.
    """
    if condition not in ("ok", "blocked"):
        return False, state
    changed = (condition, model) != (state.get("condition"), state.get("model"))
    next_state = dict(state, condition=condition, model=model)
    if condition == "blocked":
        due = REMIND_SECS > 0 and now - state.get("alert_at", 0) > REMIND_SECS
        alert = force or changed or due
    else:
        alert = force or state.get("condition") == "blocked"
    if alert:
        next_state["alert_at"] = now
    return alert, next_state


def main() -> int:
    load_env()
    report = "--report" in sys.argv
    force = "--force" in sys.argv

    model, how = session_model()
    if not model:
        # Nothing to check. Process death is systemd's alarm, not this one.
        print("no claude --channels session running - nothing to probe")
        return 0

    token, source = read_token()
    if not token:
        print("no OAuth token found - that is the auth watchdog's alarm", file=sys.stderr)
        return 0

    condition, detail = probe(token, model)
    if condition == "blocked":
        time.sleep(CONFIRM_DELAY_SECS)
        condition, detail = probe(token, model)

    usable = alternatives(token, model) if condition == "blocked" else []

    if report:
        print(f"session model: {model} ({how})")
        print(f"token source:  {source}")
        print(f"status:        {condition.upper()} - {detail}")
        if condition == "blocked":
            print("available:     " + (", ".join(usable) if usable else "none of the others"))
        return 0

    state = load_state()
    alert, next_state = decide(condition, model, state, force, time.time())
    if alert:
        if send(compose(condition, model, detail, usable)):
            save_state(next_state)
    elif next_state is not state:
        save_state(next_state)

    print(f"{model}: {condition} - {detail}")
    return 1 if condition == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
