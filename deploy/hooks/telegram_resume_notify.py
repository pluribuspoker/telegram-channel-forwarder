#!/usr/bin/env python3
"""SessionStart hook (VPS only): DM the resume command for the PREVIOUS session.

Every restart of the Telegram-channels claude session starts a fresh context
(new UUID) — the prior conversation is only recoverable via its transcript. A
planned restart lets the model post a resume pointer as its last message, but a
crash/external restart has no last message. This hook closes that gap: on each
new session it finds the previous session's transcript and DMs the user the
one-line prompt that reloads it — so resume works after ANY restart.

Gated to the VPS (hostname `pickbot`) per user request. Skips resume/clear/
compact starts (only fires on a genuine fresh startup). python3 only; never
fails the session; always exits 0.
"""
import html, json, os, re, subprocess, sys, glob, time, urllib.request, urllib.parse

PROJECT_DIR = os.path.expanduser("~/.claude/projects/-home-forwarder-app")
ACCESS = os.path.expanduser("~/.claude/channels/telegram/access.json")
ENV = os.path.expanduser("~/.claude/channels/telegram/.env")
ONLY_HOST = "pickbot"

def log(*a):
    try:
        with open("/tmp/tg_resume_notify.log", "a") as f:
            f.write(" ".join(str(x) for x in a) + "\n")
    except Exception:
        pass

def main():
    if os.environ.get("NIGHTLY_AUDIT"):
        return  # headless nightly-audit agent — not the channels session
    if os.uname().nodename != ONLY_HOST:
        return  # VPS only

    try:
        inp = json.load(sys.stdin)
    except Exception:
        inp = {}

    # Only fire on a genuine fresh start, not resume/clear/compact.
    source = (inp.get("source") or "").lower()
    if source in ("resume", "clear", "compact"):
        log("skip source", source)
        return

    cur_id = inp.get("session_id") or ""
    cur_tp = inp.get("transcript_path") or ""
    if not cur_id and cur_tp:
        cur_id = os.path.splitext(os.path.basename(cur_tp))[0]

    # Previous session = most-recently-modified transcript that isn't this one.
    files = glob.glob(os.path.join(PROJECT_DIR, "*.jsonl"))
    files = [f for f in files if os.path.splitext(os.path.basename(f))[0] != cur_id]
    if not files:
        log("no prior transcript")
        return
    prev = max(files, key=lambda f: os.path.getmtime(f))
    prev_id = os.path.splitext(os.path.basename(prev))[0]

    chat_id = _chat_id()
    if not chat_id:
        log("no chat_id")
        return
    token = _bot_token()
    if not token:
        log("no token")
        return

    # Minimal by operator request (2026-09-26): pasting it back works because
    # CLAUDE.md maps "resume <uuid>" to reading that transcript.
    msg = f"▶️ resume {prev_id}"
    # One italic cause line — unless the operator asked for this restart
    # (watchdog bot /restart etc., chat self-restart): they already know why.
    try:
        cause = restart_cause()
    except Exception as e:
        cause = None
        log("cause failed", e)
    if cause:
        msg += f"\n<i>↳ {html.escape(cause)}</i>"
    if os.environ.get("TG_RESUME_DRYRUN") == "1":
        print(f"[DRYRUN] chat={chat_id} prev={prev_id}\n{msg}")
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": msg, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=15).read()
        log("sent resume notify chat", chat_id, "prev", prev_id)
    except Exception as e:
        log("send failed", e)

UNIT = "claude-channels.service"
_START = "claude-channels: starting model="
_STOP = re.compile(r"stop-context: result=(\S+) exit=(\S+) requester=(.*?) mem avail=(\d+)MB")
_REASON = re.compile(r"claude-channels: (.+?), exiting for restart|(Failed to invoke barrier)")
# Requester-chain substrings → None (operator asked) or a label. First match wins:
# agents spawn `claude -p`, so they must precede the generic Claude-session rule.
_REQUESTERS = [
    ("claude_watchdog_bot", None),   # /restart /kill /model … restart /authcode
    ("restart_and_ping", None),      # chat self-restart (⚡→👍)
    ("needrestart", "Ubuntu auto-update"),
    ("unattended-upgrade", "Ubuntu auto-update"),
    ("hc_repair", "restarted by the hc-repair agent"),
    ("ungraded_audit", "restarted by the nightly audit agent"),
    ("trent_repair", "restarted by the trent-repair agent"),
    ("receptionist", "restarted by AK's receptionist"),
    ("claude ", "restarted by a Claude session"),
    ("sshd", "manual restart over SSH"),
]


def _journal_lines() -> list[str]:
    r = subprocess.run(
        ["journalctl", "-u", UNIT, "-b", "--since", "-12h", "-o", "cat", "--no-pager"],
        capture_output=True, text=True, timeout=10)
    keep = (_START, "stop-context:", "exiting for restart", "Failed to invoke barrier")
    return [l for l in r.stdout.splitlines() if any(k in l for k in keep)]


def _uptime_s() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def _recent_upgrades(max_age_s: int = 3600) -> list[str]:
    """Package names upgraded by apt runs that started within max_age_s."""
    try:
        blocks = open("/var/log/apt/history.log").read().strip().split("\n\n")
    except OSError:
        return []
    pkgs = []
    for block in blocks[-10:]:
        d = re.search(r"^Start-Date: (\S+ +\S+)", block, re.M)
        m = re.search(r"^Upgrade: (.*)$", block, re.M)
        try:
            started = time.mktime(time.strptime(re.sub(" +", " ", d.group(1)),
                                                "%Y-%m-%d %H:%M:%S"))
        except (AttributeError, ValueError):
            continue
        if m and time.time() - started <= max_age_s:
            for p in re.findall(r"([^\s,()]+):[\w-]+ \(", m.group(1)):
                # Family name: libheif-plugin-aomenc / libheif1 -> libheif.
                base = re.sub(r"\d*(t64)?$", "", p.split("-")[0]) or p
                if base not in pkgs:
                    pkgs.append(base)
    return pkgs


def classify(lines: list[str], uptime_s: float, upgrades: list[str]) -> str | None:
    """Why the previous session ended, from the unit's journal; None = don't say.

    The stop line comes from deploy/stop_context.py (ExecStopPost), the reason
    lines from the launcher's own exit paths."""
    starts = [i for i, l in enumerate(lines) if _START in l]
    if not starts:
        return None
    cur = starts[-1]
    prev_start = starts[-2] if len(starts) > 1 else -1
    stop = next((_STOP.search(l) for l in reversed(lines[prev_start + 1:cur])
                 if _STOP.search(l)), None)
    if not stop:
        # A reboot leaves no stop line in this boot's journal.
        return "VPS rebooted" if uptime_s < 900 else None
    result, exit_, requester, avail = stop.groups()
    if requester.startswith("none"):
        if result == "watchdog":
            return f"hung: missed systemd watchdog pings · RAM {avail}MB free"
        if result == "oom-kill":
            return f"OOM-killed · RAM {avail}MB free"
        reasons = [m for l in lines[prev_start + 1:cur] if (m := _REASON.search(l))]
        if reasons:
            why = reasons[-1].group(1) or "watchdog ping timed out"
            return f"crashed: {why} · RAM {avail}MB free"
        if result != "success":
            return f"exited on its own ({exit_}) · RAM {avail}MB free"
        return "stopped from outside, requester not traced"
    for needle, label in _REQUESTERS:
        if needle in requester:
            if label == "Ubuntu auto-update" and upgrades:
                shown = ", ".join(upgrades[:3]) + (" …" if len(upgrades) > 3 else "")
                label += f" ({shown})"
            return label
    hops = [h for h in requester.split(" < ")
            if not re.match(r"(systemctl|sudo|/bin/(ba)?sh|sh) ", h)]
    return f"restarted by: {(hops[0] if hops else requester)[:40]}"


def restart_cause() -> str | None:
    return classify(_journal_lines(), _uptime_s(), _recent_upgrades())


def _chat_id():
    try:
        d = json.load(open(ACCESS))
        allow = d.get("allowFrom") or []
        if allow:
            return str(allow[0])
    except Exception:
        pass
    return "5911202683"

def _bot_token():
    try:
        for line in open(ENV):
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get("TELEGRAM_BOT_TOKEN")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("fatal", e)
    sys.exit(0)
