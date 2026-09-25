#!/usr/bin/env python3
"""
scripts/pikkit_desktop_agent.py -- the desktop half of the phone-only Pikkit
token refresh.  Runs on the Windows desktop (real Chrome passes Turnstile;
the VPS does not), registered as a logon task, and does nothing until the
watchdog bot posts a request on the VPS (see deploy/pikkit_relay.py):

  every POLL_IDLE seconds     ssh: write the heartbeat, read request.json
  request "requested"         claim it, run scripts/pikkit_page_login.py in
                              real Chrome, DM "SMS sent" via the watchdog bot
  request "code_relayed"      hand the code to the login script
  login finished              install the token on the VPS through
                              scripts/set_env_local.py (stdin, never argv),
                              validate there, DM the result, mark done/failed

The token never touches the relay or this machine's .env.local.  One SSH
session per poll (heartbeat + read in one call); the VPS root key already on
this desktop is what it uses.  Single instance via a localhost port lock.
Log: logs/pikkit_desktop_agent.log (gitignored).

Manual run:   python scripts/pikkit_desktop_agent.py --once   (one poll, then exit)
Logon task:   see docs/odds.md "Pikkit token from a phone".
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

import pikkit_relay as relay  # noqa: E402

VPS = os.environ.get("PIKKIT_AGENT_VPS", "root@209.38.51.86")
REMOTE_DIR = os.environ.get("PIKKIT_RELAY_DIR", "/home/forwarder/pikkit_relay")
REMOTE_APP = "/home/forwarder/app"
REMOTE_PY = "/home/forwarder/venv/bin/python"
PHONE = os.environ.get("PIKKIT_PHONE", "+19545361686")
POLL_IDLE = 30
POLL_ACTIVE = 3
LOGIN_TIMEOUT = 600
LOCK_PORT = 48731
WORK_DIR = Path(os.environ.get("LOCALAPPDATA") or ROOT / "logs") / "pikkit_agent"
LOG_FILE = ROOT / "logs" / "pikkit_desktop_agent.log"
LOGIN_SCRIPT = ROOT / "scripts" / "pikkit_page_login.py"

log = logging.getLogger("pikkit-agent")


def _ssh_binary() -> str:
    win = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
    if win.exists():
        return str(win)
    return shutil.which("ssh") or "ssh"


def ssh(script: str, stdin: str | None = None, timeout: int = 40) -> tuple[int, str]:
    """Run a shell snippet on the VPS; returns (rc, combined output)."""
    argv = [_ssh_binary(), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", VPS, script]
    try:
        r = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "(ssh timed out)"
    except OSError as e:
        return 127, f"(ssh failed to start: {e})"
    return r.returncode, (r.stdout + r.stderr).strip()


# -- relay over ssh -----------------------------------------------------------


def poll(now: float) -> dict | None:
    """Heartbeat + read the request in one SSH session."""
    rc, out = ssh(
        f"mkdir -p {REMOTE_DIR} && printf '%s\\n' {int(now)} > {REMOTE_DIR}/heartbeat.tmp "
        f"&& mv {REMOTE_DIR}/heartbeat.tmp {REMOTE_DIR}/heartbeat && chown -R forwarder:forwarder {REMOTE_DIR}; "
        f"cat {REMOTE_DIR}/request.json 2>/dev/null; exit 0"  # no request file is not a failure
    )
    if rc != 0:
        log.warning("poll failed rc=%s: %s", rc, out[-300:])
        return None
    try:
        data = json.loads(out) if out.strip() else None
    except ValueError:
        log.warning("request.json unparseable: %r", out[:200])
        return None
    return data if isinstance(data, dict) and data.get("state") else None


def read_request() -> dict | None:
    rc, out = ssh(f"cat {REMOTE_DIR}/request.json 2>/dev/null; exit 0")
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_request(req: dict) -> bool:
    rc, out = ssh(
        f"cat > {REMOTE_DIR}/request.json.tmp && mv {REMOTE_DIR}/request.json.tmp "
        f"{REMOTE_DIR}/request.json && chown forwarder:forwarder {REMOTE_DIR}/request.json",
        stdin=json.dumps(req, indent=2) + "\n",
    )
    if rc != 0:
        log.warning("write_request failed rc=%s: %s", rc, out[-300:])
    return rc == 0


def dm(text: str) -> None:
    token = os.environ.get("WATCHDOG_BOT_TOKEN", "")
    uid = os.environ.get("WATCHDOG_USER_ID", "")
    if not token or not uid:
        log.warning("no WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID -- cannot DM: %s", text)
        return
    import urllib.parse
    import urllib.request

    data = urllib.parse.urlencode({"chat_id": uid, "text": text}).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data),
            timeout=20,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("DM failed: %s", e)


def install_token(token: str) -> tuple[bool, str]:
    """Write PIKKIT_TOKEN on the VPS via set_env_local.py (token on stdin) and validate."""
    rc, out = ssh(
        'T=$(cat); [ ${#T} -ge 16 ] || { echo "bad token length ${#T}"; exit 1; }; '
        f'cd {REMOTE_APP} && python3 scripts/set_env_local.py "PIKKIT_TOKEN=$T" >/dev/null '
        f"&& {REMOTE_PY} scripts/pikkit_auth.py --validate",
        stdin=token,
        timeout=90,
    )
    return rc == 0 and "Token valid" in out, out[-400:]


# -- one login ----------------------------------------------------------------


def run_login(req: dict) -> None:
    now = time.time()
    req = relay.advance(req, "claimed", now)
    if not write_request(req):
        return
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    code_file = WORK_DIR / "code.txt"
    token_file = WORK_DIR / "token.txt"
    debug_dir = WORK_DIR / "debug"
    for f in (code_file, token_file):
        f.unlink(missing_ok=True)

    argv = [
        sys.executable, str(LOGIN_SCRIPT), "--phone", PHONE, "--code-file", str(code_file),
        "--token-file", str(token_file), "--wait", "420", "--debug-dir", str(debug_dir),
    ]
    log.info("starting login for request %s", req.get("id"))
    proc = subprocess.Popen(
        argv, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lines: list[str] = []

    def reader():
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            lines.append(line)
            log.info("login: %s", line)

    threading.Thread(target=reader, daemon=True).start()

    sms_announced = False
    code_written = False
    deadline = time.time() + LOGIN_TIMEOUT
    while proc.poll() is None and time.time() < deadline:
        if not sms_announced and any("SMS sent" in ln for ln in lines):
            sms_announced = True
            req = relay.advance(req, "sms_sent", time.time())
            write_request(req)
            dm("\U0001f4f2 Pikkit SMS sent to your phone. Reply /pikkitcode <8 digits> within 5 minutes.")
        if sms_announced and not code_written:
            remote = read_request()
            if remote and remote.get("state") == "cancelled":
                log.info("request cancelled remotely; stopping login")
                proc.kill()
                break
            if remote and remote.get("state") == "code_relayed" and remote.get("code"):
                code_file.write_text(str(remote["code"]))
                code_written = True
                req = remote
                log.info("code handed to the login script")
        time.sleep(POLL_ACTIVE)

    if proc.poll() is None:
        proc.kill()
        log.warning("login timed out after %ss", LOGIN_TIMEOUT)

    token = token_file.read_text().strip() if token_file.exists() else ""
    token_file.unlink(missing_ok=True)
    code_file.unlink(missing_ok=True)
    now = time.time()
    if token:
        ok, detail = install_token(token)
        if ok:
            write_request(relay.advance(req, "done", now))
            dm("\u2705 Pikkit token refreshed: installed on the VPS and valid. Splits resume on the next tracker pass.")
            log.info("token installed and valid")
        else:
            write_request(relay.advance(req, "failed", now, error=f"install/validate: {detail[-160:]}"))
            dm(f"\U0001f6ab Pikkit login worked but installing on the VPS failed:\n{detail[-600:]}")
            log.error("install failed: %s", detail)
        return
    tail = " | ".join(lines[-3:])[-400:] or "no output"
    state = "expired" if (sms_announced and not code_written) else "failed"
    write_request(relay.advance(req, state, now, error=tail))
    if state == "expired":
        dm("\u23f3 Pikkit refresh expired: no code arrived in time. Send /pikkit to try again.")
    else:
        dm(f"\U0001f6ab Pikkit refresh failed on the desktop:\n{tail}\nSend /pikkit to try again.")
    log.error("login %s: %s", state, tail)


# -- main loop ----------------------------------------------------------------


def _lock() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="one poll (and login if requested), then exit")
    args = ap.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    if sys.stdout and sys.stdout.isatty():
        log.addHandler(logging.StreamHandler(sys.stdout))

    lock = _lock()
    if lock is None:
        log.info("another agent holds the lock port; exiting")
        return 0
    log.info("agent up: vps=%s relay=%s phone=%s", VPS, REMOTE_DIR, PHONE[-4:])

    while True:
        now = time.time()
        req = poll(now)
        if req and req.get("state") == "requested" and relay.is_active(req, now):
            run_login(req)
        if args.once:
            return 0
        time.sleep(POLL_IDLE)


if __name__ == "__main__":
    raise SystemExit(main())
