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

Requires: pip install python-telegram-bot (already in venv)
Env: WATCHDOG_BOT_TOKEN, WATCHDOG_USER_ID in .env
"""

import os
import subprocess
import asyncio
import logging
from pathlib import Path

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

    text = (msg.text or "").strip().lower()

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

    elif text == "/help":
        await msg.reply_text(
            "Emergency watchdog commands:\n"
            "/ping — liveness check\n"
            "/mem — live RAM + swap usage (alias /ram)\n"
            "/status — service status\n"
            "/restart — restart claude-channels\n"
            "/kill — force-kill and restart\n"
            "/logs — last 20 journal lines\n"
            "/tmux — see what Claude is doing right now"
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
