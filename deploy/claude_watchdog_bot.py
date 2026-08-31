#!/usr/bin/env python3
"""
Emergency watchdog bot for Claude Code Telegram channels.

A separate, minimal Telegram bot that runs independently from Claude Code.
It uses its own bot token and provides a "break glass" escape hatch when
the main Claude bot is stuck (context full, hanging, plan mode, etc.).

Commands (only responds to ALLOWED_USER_ID):
  /restart  — restart the claude-channels service
  /status   — show service status + last activity
  /logs     — show last 20 lines of claude-channels journal
  /kill     — force-kill all claude/bun processes and restart
  /ping     — responds "pong" (liveness check)
  /mem      — live RAM + swap usage + top consumers (alias /ram)
  /tmux     — capture last 50 lines of Claude's tmux pane (see what it's doing)
  /reauth   — mint a new 1-year OAuth token (start; DMs a login URL)
  /authcode — finish re-auth by pasting the code from that URL

Re-auth lives here rather than in Claude itself for the obvious reason: it is
needed precisely when Claude can't answer. This bot is a separate service with
its own token, so it survives both a dead credential and the claude-channels
restart that fixing one requires.

Requires: pip install python-telegram-bot (already in venv)
Env: WATCHDOG_BOT_TOKEN, WATCHDOG_USER_ID in .env
"""

import os
import re
import subprocess
import asyncio
import logging
import time
from pathlib import Path

from claude_auth_watchdog import AUTH_ENV, probe as auth_probe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("watchdog")

# Load .env
env_file = Path(__file__).resolve().parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        m = line.strip()
        if m and not m.startswith("#") and "=" in m:
            k, v = m.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

TOKEN = os.environ.get("WATCHDOG_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("WATCHDOG_USER_ID", "0"))
SERVICE = "claude-channels.service"

if not TOKEN or not ALLOWED_USER_ID:
    print("Set WATCHDOG_BOT_TOKEN and WATCHDOG_USER_ID in .env")
    raise SystemExit(1)


def run(cmd: str, timeout: int = 30) -> str:
    """Run a shell command and return output."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return "(command timed out)"


def run_argv(argv: list[str], timeout: int = 30) -> str:
    """Run without a shell. Used wherever a Telegram-supplied string is an argument."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return "(command timed out)"


# --- re-auth -------------------------------------------------------------
# `claude setup-token` is interactive: it prints a login URL, then blocks on
# stdin for the code. Driving it from Telegram means holding that process open
# across two messages, so it runs in a detached tmux session.
#
# Two properties of that session are load-bearing, both learned the hard way on
# 2026-08-21:
#   * a DEDICATED SOCKET (-L). On tmux's default socket the server is a child of
#     claude-channels.service, so KillMode=control-group destroys it on any
#     restart — including the restart re-auth itself performs at the end.
#   * a WIDE PANE (-x 400). The CLI hard-wraps output to the pane width, and a
#     token split across two lines gets silently truncated on capture. An 79-of-108
#     character token looks like a token, writes cleanly, and only fails later.
AUTH_SOCKET = "authtok"
AUTH_SESSION = "setuptok"
TOKEN_RE = re.compile(r"sk-ant-oat\d*-[A-Za-z0-9_-]+")
URL_RE = re.compile(r"https://claude\.com/\S+")
# Codes are <code>#<state>, both URL-safe base64. Anything else is not a code.
CODE_RE = re.compile(r"^[A-Za-z0-9_\-#]{20,400}$")


def _auth_pane() -> str:
    return run(f"tmux -L {AUTH_SOCKET} capture-pane -t {AUTH_SESSION} -p -J -S -80")


def start_reauth() -> str:
    """Launch `claude setup-token` in an isolated pty; return the login URL or an error."""
    run(f"tmux -L {AUTH_SOCKET} kill-server 2>/dev/null; true")
    run(
        f"setsid tmux -L {AUTH_SOCKET} new-session -d -x 400 -y 50 -s {AUTH_SESSION} "
        f"'claude setup-token; sleep 900'"
    )
    for _ in range(12):
        time.sleep(2)
        m = URL_RE.search(_auth_pane())
        if m:
            return m.group(0)
    return ""


def finish_reauth(code: str) -> str:
    """Feed the code in, verify the resulting token, install it, restart. Returns a report."""
    if "no server running" in run(f"tmux -L {AUTH_SOCKET} has-session -t {AUTH_SESSION} 2>&1"):
        return "❌ No re-auth in progress (or it expired). Send /reauth to start over."

    run_argv(["tmux", "-L", AUTH_SOCKET, "send-keys", "-t", AUTH_SESSION, code, "Enter"])
    time.sleep(3)
    # The CLI's masked prompt sometimes swallows the first Enter.
    run_argv(["tmux", "-L", AUTH_SOCKET, "send-keys", "-t", AUTH_SESSION, "Enter"])

    token = ""
    for _ in range(10):
        time.sleep(2)
        m = TOKEN_RE.search(_auth_pane())
        if m:
            token = m.group(0)
            break
    if not token:
        pane = _auth_pane()[-400:]
        return f"❌ No token appeared. Last output:\n\n{pane}"
    if len(token) < 100:
        # Truncation guard: never install a token we only partly captured.
        return f"❌ Captured token is only {len(token)} chars — refusing to install it. Try /reauth again."

    # Verify BEFORE installing. Writing an unverified token would swap a working
    # credential for a broken one and take the session down.
    dead, detail = auth_probe(token)
    if dead:
        return f"❌ New token failed its check ({detail}). Nothing was changed."

    tmp = AUTH_ENV.with_suffix(".tmp")
    tmp.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={token}\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_ENV)
    run(f"tmux -L {AUTH_SOCKET} kill-server 2>/dev/null; true")

    run(f"sudo -n systemctl restart {SERVICE}", timeout=60)
    time.sleep(15)
    status = run(f"systemctl is-active {SERVICE}")
    bun = "yes" if run("pgrep -f 'bun server'") else "no"
    return (
        f"✅ New 1-year token installed and verified.\n\n"
        f"Service: {status}\nBun running: {bun}\n\n"
        f"Claude's context was reset by the restart. Send it a fresh message and "
        f"wait for the 👀 before firing a real task."
    )


def mem_summary() -> str:
    """Live RAM + swap snapshot with the top consumers and a health flag."""
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        p = line.split()
        if len(p) >= 2:
            info[p[0].rstrip(":")] = int(p[1])  # kB
    tot = info.get("MemTotal", 0) // 1024
    avail = info.get("MemAvailable", 0) // 1024
    used = tot - avail
    stot = info.get("SwapTotal", 0) // 1024
    sused = (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) // 1024

    # top RSS consumers (real command, truncated)
    top_lines = []
    raw = run("ps -eo rss,args --sort=-rss --no-headers | head -6")
    for l in raw.splitlines():
        parts = l.split(None, 1)
        if len(parts) == 2:
            rss_mb = int(parts[0]) // 1024
            cmd = parts[1][:42]
            top_lines.append(f"  {rss_mb:>4}MB  {cmd}")

    if sused > 1024:
        flag = "🟡 swap under pressure"
    elif avail < 80:
        flag = "🟡 low free RAM"
    else:
        flag = "🟢 healthy"

    swap_note = f"{sused}MB used / {stot}MB" if stot else "none configured"
    return (
        f"🧠 Memory — {flag}\n"
        f"RAM:  {used}MB used / {tot}MB  ({avail}MB available)\n"
        f"Swap: {swap_note}\n"
        f"Top:\n" + "\n".join(top_lines)
    )


async def handle_message(update, context):
    """Handle incoming messages."""
    msg = update.message
    if not msg or msg.from_user.id != ALLOWED_USER_ID:
        return

    raw = (msg.text or "").strip()
    # Match commands case-insensitively, but keep `raw` for arguments: an auth
    # code is case-sensitive base64 and lowercasing it silently destroys it.
    text = raw.lower()

    if text == "/ping":
        await msg.reply_text("pong")

    elif text in ("/mem", "/ram"):
        await msg.reply_text(f"```\n{mem_summary()}\n```", parse_mode="Markdown")

    elif text == "/status":
        status = run(f"systemctl status {SERVICE} 2>&1 | head -12")
        # Check last bun activity
        bun_check = run("ps aux | grep 'bun server' | grep -v grep | head -1")
        reply = f"```\n{status}\n```"
        if bun_check:
            reply += f"\n\nBun: running"
        else:
            reply += f"\n\nBun: NOT running"
        await msg.reply_text(reply, parse_mode="Markdown")

    elif text == "/restart":
        await msg.reply_text("Restarting claude-channels...")
        out = run(f"sudo -n systemctl restart {SERVICE}", timeout=60)
        await asyncio.sleep(15)
        status = run(f"systemctl is-active {SERVICE}")
        bun = "yes" if run("pgrep -f 'bun server'") else "no"
        await msg.reply_text(
            f"Service: {status}\nBun running: {bun}\n{out or '(clean restart)'}"
        )

    elif text == "/kill":
        await msg.reply_text("Force-killing all claude/bun processes and restarting...")
        run("sudo -n systemctl stop claude-channels")
        run("pkill -9 -f 'claude --channels' || true")
        run("pkill -9 -f 'bun server.ts' || true")
        await asyncio.sleep(3)
        run(f"sudo -n systemctl start {SERVICE}")
        await asyncio.sleep(15)
        status = run(f"systemctl is-active {SERVICE}")
        bun = "yes" if run("pgrep -f 'bun server'") else "no"
        await msg.reply_text(f"Service: {status}\nBun running: {bun}")

    elif text == "/logs":
        logs = run(f"journalctl -u {SERVICE} --no-pager -n 20 2>&1")
        # Truncate to Telegram's 4096 char limit
        if len(logs) > 4000:
            logs = logs[-4000:]
        await msg.reply_text(f"```\n{logs}\n```", parse_mode="Markdown")

    elif text == "/tmux":
        out = run("tmux capture-pane -t claude -p -S -50 2>&1")
        if not out:
            out = "(empty pane or no tmux session)"
        if len(out) > 4000:
            out = out[-4000:]
        await msg.reply_text(f"```\n{out}\n```", parse_mode="Markdown")

    elif text == "/reauth":
        await msg.reply_text("Starting re-auth — minting a new 1-year token...")
        url = await asyncio.to_thread(start_reauth)
        if not url:
            await msg.reply_text(
                "❌ Couldn't get a login URL. Check `/logs` or run "
                "`claude setup-token` over SSH."
            )
        else:
            await msg.reply_text(
                f"1. Open this link and approve:\n\n{url}\n\n"
                f"2. Copy the code it gives you and send it back as:\n"
                f"/authcode <code>\n\n"
                f"(Link is good for 15 minutes.)",
                disable_web_page_preview=True,
            )

    elif text.startswith("/authcode"):
        parts = raw.split(None, 1)
        if len(parts) < 2:
            await msg.reply_text("Usage: /authcode <code from the login page>")
        elif not CODE_RE.match(parts[1].strip()):
            await msg.reply_text("That doesn't look like an auth code. Paste the whole string.")
        else:
            await msg.reply_text("Verifying and installing...")
            result = await asyncio.to_thread(finish_reauth, parts[1].strip())
            await msg.reply_text(result, disable_web_page_preview=True)

    elif text == "/auth":
        out = await asyncio.to_thread(
            run,
            "/home/forwarder/venv/bin/python "
            "/home/forwarder/app/deploy/claude_auth_watchdog.py --report",
            60,
        )
        await msg.reply_text(f"```\n{out or '(no output)'}\n```", parse_mode="Markdown")

    elif text == "/help":
        await msg.reply_text(
            "Emergency watchdog commands:\n"
            "/ping — liveness check\n"
            "/mem — live RAM + swap usage (alias /ram)\n"
            "/status — service status\n"
            "/restart — restart claude-channels\n"
            "/kill — force-kill and restart\n"
            "/logs — last 20 journal lines\n"
            "/tmux — see what Claude is doing right now\n"
            "/auth — check whether Claude's credentials still work\n"
            "/reauth — mint a new 1-year token (fixes \"Login expired\")"
        )


def main():
    from telegram.ext import ApplicationBuilder, MessageHandler, filters

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.COMMAND, handle_message))
    log.info("Watchdog bot started (user_id=%d)", ALLOWED_USER_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
