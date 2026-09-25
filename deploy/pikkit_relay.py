"""
deploy/pikkit_relay.py -- the request relay between the watchdog bot and the
desktop agent that refreshes the Pikkit token from a phone.

Why a relay: Pikkit's login is gated by Cloudflare Turnstile, which passes in
real Chrome on the desktop and fails on the VPS (datacenter IP, no IPv6 route
to the challenge's probe host, no GPU -- "Please complete verification and try
again", 2026-09-24).  So the browser step runs on the desktop, and Telegram
drives it through two files in RELAY_DIR on the VPS:

  request.json  -- one login request at a time, a small state machine:
                   requested -> claimed -> sms_sent -> code_relayed -> done
                   (or failed / expired / cancelled).  The bot creates it on
                   /pikkit and adds the code on /pikkitcode; the desktop agent
                   moves every other state.
  heartbeat     -- epoch seconds, written by the agent on every poll, so
                   /pikkit can say up front whether the desktop is reachable.

The bot (forwarder) and the agent (root over SSH) both read and write these
as plain files; every write is atomic (tmp + rename).  The token itself never
passes through the relay: the agent installs it straight into .env.local via
scripts/set_env_local.py over the same SSH session.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

RELAY_DIR = Path(os.environ.get("PIKKIT_RELAY_DIR") or "/home/forwarder/pikkit_relay")
REQUEST_FILE = "request.json"
HEARTBEAT_FILE = "heartbeat"

ACTIVE_STATES = ("requested", "claimed", "sms_sent", "code_relayed")
FINAL_STATES = ("done", "failed", "expired", "cancelled")
REQUEST_TTL = 10 * 60          # a request older than this is stale whatever its state
AGENT_ONLINE_WINDOW = 90       # heartbeat age that still counts as "online"
PICKUP_TIMEOUT = 120           # bot DMs a warning if nobody claims within this
CODE_RE = re.compile(r"^\d{6,8}$")


# -- files -----------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def read_request(relay_dir: Path = RELAY_DIR) -> dict | None:
    try:
        data = json.loads((relay_dir / REQUEST_FILE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("state") else None


def write_request(req: dict, relay_dir: Path = RELAY_DIR) -> None:
    _atomic_write(relay_dir / REQUEST_FILE, json.dumps(req, indent=2) + "\n")


def read_heartbeat(relay_dir: Path = RELAY_DIR) -> float | None:
    try:
        return float((relay_dir / HEARTBEAT_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def write_heartbeat(now: float, relay_dir: Path = RELAY_DIR) -> None:
    _atomic_write(relay_dir / HEARTBEAT_FILE, f"{now:.0f}\n")


# -- semantics -------------------------------------------------------------


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def agent_status(now: float, relay_dir: Path = RELAY_DIR) -> tuple[bool, str]:
    """(online, human line) from the heartbeat file."""
    seen = read_heartbeat(relay_dir)
    if seen is None:
        return False, "desktop agent: never seen"
    age = now - seen
    if age <= AGENT_ONLINE_WINDOW:
        return True, f"desktop agent: online ({_age(age)} ago)"
    return False, f"desktop agent: OFFLINE (last seen {_age(age)} ago)"


def is_active(req: dict | None, now: float) -> bool:
    if not req or req.get("state") not in ACTIVE_STATES:
        return False
    return now - float(req.get("requested_at") or 0) < REQUEST_TTL


def new_request(now: float) -> dict:
    return {"id": f"{int(now)}", "state": "requested", "requested_at": now}


def advance(req: dict, state: str, now: float, **fields) -> dict:
    """Return a copy of req moved to `state`, stamping <state>_at."""
    out = dict(req)
    out["state"] = state
    out[f"{state}_at"] = now
    out.update(fields)
    return out


def describe(req: dict | None, now: float) -> str:
    if not req:
        return "no Pikkit login in progress"
    state = req.get("state", "?")
    age = _age(now - float(req.get("requested_at") or now))
    lines = {
        "requested": f"requested {age} ago, waiting for the desktop to pick it up",
        "claimed": f"desktop is opening Chrome (requested {age} ago)",
        "sms_sent": f"SMS sent -- reply /pikkitcode <8 digits> (requested {age} ago)",
        "code_relayed": "code relayed, desktop is finishing the login",
        "done": f"done -- token installed and valid ({age} ago)",
        "failed": f"failed {age} ago: {req.get('error') or 'see the agent log'}",
        "expired": f"expired {age} ago (no code in time)",
        "cancelled": f"cancelled {age} ago",
    }
    return lines.get(state, f"{state} ({age} ago)")


def relay_code(code: str, now: float, relay_dir: Path = RELAY_DIR) -> tuple[bool, str]:
    """Attach the SMS code to the active request. Returns (ok, reply)."""
    if not CODE_RE.match(code):
        return False, "That doesn't look like a Pikkit code (6-8 digits). Usage: /pikkitcode 12345678"
    req = read_request(relay_dir)
    if not is_active(req, now):
        return False, "No Pikkit login in progress -- send /pikkit first."
    if req["state"] == "code_relayed":
        return False, "A code is already relayed; wait for the desktop's result."
    if req["state"] not in ("claimed", "sms_sent"):
        return False, f"Not ready for a code yet ({describe(req, now)})."
    write_request(advance(req, "code_relayed", now, code=code), relay_dir)
    return True, "Code relayed to the desktop -- you'll get a result DM within a minute."


def start_request(now: float, relay_dir: Path = RELAY_DIR) -> tuple[bool, str]:
    """Create a request unless one is active. Returns (started, reply)."""
    online, agent_line = agent_status(now, relay_dir)
    req = read_request(relay_dir)
    if is_active(req, now):
        return False, f"Already in progress: {describe(req, now)}\n{agent_line}\n(/pikkit cancel to abandon it)"
    write_request(new_request(now), relay_dir)
    reply = (
        "Pikkit token refresh requested.\n"
        f"{agent_line}\n\n"
        "1. The desktop opens Chrome and sends the SMS -- you get a DM when it's out.\n"
        "2. Reply /pikkitcode <8 digits> within 5 minutes.\n"
        "3. The desktop installs and validates the token on the VPS and DMs the result."
    )
    if not online:
        reply += "\n\n⚠️ The desktop agent isn't checking in -- the desktop must be on and logged in for this to work."
    return True, reply


def cancel_request(now: float, relay_dir: Path = RELAY_DIR) -> str:
    req = read_request(relay_dir)
    if not is_active(req, now):
        return "Nothing to cancel."
    write_request(advance(req, "cancelled", now), relay_dir)
    return "Cancelled. Send /pikkit to start over."
