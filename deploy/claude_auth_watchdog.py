"""Claude auth watchdog — DMs the operator when the VPS Claude session's credentials die.

Stays silent unless auth is actually broken, then alerts through the watchdog bot
(same bot as mem_watchdog / claude_spend_watchdog).

Why this exists: the credentials Claude Code writes to ~/.claude/.credentials.json
carry a refresh token with a hard expiry, and when it passes, the CLI clears the
blob to empty strings and there is no way back without a browser. That happened on
2026-08-11 01:26 UTC (the exact refreshTokenExpiresAt in the file). Nothing
reported it. The Telegram session answered "Login expired · Please run /login" to
every message, systemd still called the unit `active` — the process was healthy,
only its credentials weren't — and the outage ran ~10 days until the operator next
reached a desktop. The 1-year CLAUDE_CODE_OAUTH_TOKEN now in auth.env makes this
rare; this makes it *visible*, which is the part that actually cost the 10 days.

Probes the API directly instead of shelling out to `claude`. A second CLI process
on this box perturbs the channels plugin's bun poller, and on 2026-08-21 that
tripped run_claude_channels.sh's "bun/telegram plugin gone" check and restarted
claude-channels.service mid-check — a monitor that reboots the thing it watches is
worse than no monitor. One 1-token haiku call, ~$0.00002 per probe.

Only 401/403 counts as dead. A 429, a 5xx, or a network error is NOT an auth
failure and must stay silent, for the same reason an ESPN outage must never read
as a bad parse: an alert that cries wolf on every blip gets muted, and then the
real one is invisible too.

Reuses WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID from .env. State (for debounce)
lives in ~/.claude_auth_watchdog_state.json.

Usage:
    python deploy/claude_auth_watchdog.py            # normal (silent unless dead)
    python deploy/claude_auth_watchdog.py --report   # print the result, send nothing
    python deploy/claude_auth_watchdog.py --force    # send even if healthy / debounced
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
STATE = Path.home() / ".claude_auth_watchdog_state.json"

AUTH_ENV = Path(os.environ.get("CLAUDE_AUTH_ENV", Path.home() / ".claude" / "auth.env"))
CREDS = Path.home() / ".claude" / ".credentials.json"

ALERT_DEBOUNCE_SECS = 12 * 60 * 60  # at most one 🔑 alert per 12h
PROBE_MODEL = "claude-haiku-4-5-20251001"

# Claude Code's own system prompt. OAuth (subscription) tokens are only accepted
# on requests that identify as Claude Code; without this the API 401s on a token
# that is in fact perfectly valid, which would page the operator over nothing.
CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."


def load_env() -> None:
    for name in (".env", ".env.local"):
        f = APP / name
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            m = line.strip()
            if m and not m.startswith("#") and "=" in m:
                k, v = m.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (OSError, ValueError):
            pass
    return {}


def save_state(s: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    os.replace(tmp, STATE)


def send(text: str) -> bool:
    token = os.environ.get("WATCHDOG_BOT_TOKEN", "")
    uid = os.environ.get("WATCHDOG_USER_ID", "")
    if not token or not uid:
        print("WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID not set", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({"chat_id": uid, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001 - never let a send failure kill the check
        print(f"send failed: {e}", file=sys.stderr)
        return False


def read_token() -> tuple[str, str]:
    """(token, source). auth.env wins — it is what the service actually runs on."""
    if AUTH_ENV.exists():
        for line in AUTH_ENV.read_text().splitlines():
            m = line.strip()
            if m.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                tok = m.split("=", 1)[1].strip().strip("'\"")
                if tok:
                    return tok, str(AUTH_ENV)
    # Fallback: the self-expiring blob. Reading it here is deliberate — if the
    # service ever falls back to it, the watchdog follows, so the probe always
    # tests the credential actually in use.
    if CREDS.exists():
        try:
            oauth = json.loads(CREDS.read_text()).get("claudeAiOauth", {})
            tok = (oauth.get("accessToken") or "").strip()
            if tok:
                return tok, str(CREDS)
        except (OSError, ValueError):
            pass
    return "", "none"


def probe(token: str) -> tuple[bool, str]:
    """(is_auth_failure, detail). Never raises."""
    body = json.dumps({
        "model": PROBE_MODEL,
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
            return False, f"HTTP {r.status} — auth OK"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        if e.code in (401, 403):
            return True, f"HTTP {e.code} — {detail or 'authentication rejected'}"
        # Rate limits, overloads, outages: not an auth problem. Stay silent.
        return False, f"HTTP {e.code} (not an auth failure) — {detail}"
    except Exception as e:  # noqa: BLE001
        return False, f"probe could not reach the API ({e}) — not treated as auth failure"


def main() -> int:
    load_env()
    report = "--report" in sys.argv
    force = "--force" in sys.argv

    token, source = read_token()
    if not token:
        dead, detail = True, "no token found in auth.env or .credentials.json"
    else:
        dead, detail = probe(token)
        if dead:
            # One retry: a single 401 is unambiguous, but a retry costs ~nothing
            # and rules out a freak edge at the edge of a token rotation.
            time.sleep(10)
            dead, detail = probe(token)

    status = "DEAD" if dead else "OK"
    if report:
        print(f"token source: {source}")
        print(f"token: {token[:18] + '…' if token else '(none)'} ({len(token)} chars)")
        print(f"auth: {status} — {detail}")
        return 0

    state = load_state()
    now = time.time()

    if not dead:
        if state.get("alerted_at"):
            send("🔑 Claude auth on the VPS is working again.")
            state.pop("alerted_at", None)
            save_state(state)
        if force:
            send(f"🔑 Claude auth check (forced): OK — {detail}")
        print(f"auth OK — {detail}")
        return 0

    print(f"auth DEAD — {detail}", file=sys.stderr)
    if force or now - state.get("alerted_at", 0) > ALERT_DEBOUNCE_SECS:
        sent = send(
            "🔑 Claude auth is DEAD on the VPS.\n\n"
            f"{detail}\n"
            f"source: {source}\n\n"
            "The Telegram session will answer \"Login expired\" to every message.\n\n"
            "Fix it from your phone: send /reauth to this bot."
        )
        if sent:
            state["alerted_at"] = now
            save_state(state)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
