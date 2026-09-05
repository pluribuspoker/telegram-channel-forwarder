#!/usr/bin/env python3
"""env_backup.py — snapshot + validate .env / .env.local, alert on a bad write.

Triggered by env-backup.path the instant either file changes, and by
env-backup.timer every 30 min as a backstop (inotify can miss an atomic
rename-into-place). On each run, for every target file that exists:

  1. VALIDATE it:
       * every *_SESSION value must parse as a Telethon StringSession;
       * no key present in the last known-good backup may have gone missing.
  2. If GOOD and the content changed since the latest backup -> write a new
     timestamped snapshot to /home/forwarder/env-backups (mode 600), then
     prune to the newest KEEP copies.
  3. If BAD -> do NOT snapshot it (that would overwrite the good history);
     save a forensic <name>.<ts>.BAD copy, and DM the operator via the
     watchdog bot with exactly what is wrong and how to recover.

This exists because on 2026-09-05 a hand-edit clobbered .env.local (dropped 4
keys, corrupted TELEGRAM_SESSION) and there was NO backup — recovery only
worked because the listener happened to still be running with the old values
in memory (/proc/<pid>/environ). This removes that luck: a one-line `cp` from
the newest good snapshot restores it, and a bad write pages within seconds.

Silent unless something is wrong (same contract as the other watchdogs).
Reuses WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID. Stdlib + optional telethon.

Usage:
  python deploy/env_backup.py            # one cycle (path unit + timer call this)
  python deploy/env_backup.py --test     # send a liveness DM and exit
"""

import hashlib
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
BACKUP_DIR = Path.home() / "env-backups"
TARGETS = [".env", ".env.local"]
KEEP = 15  # newest good snapshots to retain per file
SESSION_SUFFIX = "_SESSION"


def load_env() -> None:
    """Populate os.environ from .env for WATCHDOG_* when run outside systemd."""
    f = APP / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        m = line.strip()
        if m and not m.startswith("#") and "=" in m:
            k, v = m.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


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
    except Exception as e:  # noqa: BLE001
        print(f"send failed: {e}", file=sys.stderr)
        return False


def parse_env(text: str) -> dict:
    d = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        d[k.strip()] = v.rstrip("\n")
    return d


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def snapshots_for(name: str):
    """Existing good snapshots for `name`, oldest-first.

    Exact match on the timestamp suffix, NOT a glob — glob(".env.*.bak") also
    matches ".env.local.*.bak", which made .env validate against .env.local's
    keyset and false-alarm on every run.
    """
    pat = re.compile(re.escape(name) + r"\.\d{8}_\d{6}\.bak$")
    return sorted(p for p in BACKUP_DIR.iterdir() if pat.match(p.name))


def validate(name: str, text: str):
    """Return list of human-readable problems ([] == good)."""
    problems = []
    env = parse_env(text)

    # 1. session parseability
    try:
        from telethon.sessions import StringSession
        have_telethon = True
    except ImportError:
        have_telethon = False
    if have_telethon:
        for k, v in env.items():
            if k.endswith(SESSION_SUFFIX):
                try:
                    StringSession(v)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{k} won't parse as a session ({type(e).__name__})")

    # 2. dropped-key check vs latest good snapshot
    snaps = snapshots_for(name)
    if snaps:
        prev = parse_env(snaps[-1].read_text())
        missing = set(prev) - set(env)
        if missing:
            problems.append("keys dropped since last good backup: "
                            + ", ".join(sorted(missing)))
    return problems


def prune(name: str) -> None:
    snaps = snapshots_for(name)
    for old in snaps[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def process(target: Path) -> bool:
    """Handle one file. Return True if a bad write was detected."""
    name = target.name
    raw = target.read_bytes()
    problems = validate(name, raw.decode(errors="replace"))
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if problems:
        bad = BACKUP_DIR / f"{name}.{ts}.BAD"
        bad.write_bytes(raw)
        os.chmod(bad, 0o600)
        snaps = snapshots_for(name)
        latest = snaps[-1].name if snaps else "(none)"
        msg = (
            f"🔴 {name} FAILED validation:\n"
            + "\n".join(f"  • {p}" for p in problems)
            + f"\n\nBad copy saved: env-backups/{bad.name}"
            + f"\nRestore the last good one:\n"
            + f"  cp ~/env-backups/{latest} {APP}/{name}"
            + f"\n(then: sudo -n systemctl restart telegram-tracker)"
        )
        print(msg)
        send(msg)
        return True

    # good — snapshot only if content changed since the newest backup
    snaps = snapshots_for(name)
    if snaps and sha(snaps[-1].read_bytes()) == sha(raw):
        print(f"  {name}: unchanged, no new snapshot")
        return False
    dst = BACKUP_DIR / f"{name}.{ts}.bak"
    dst.write_bytes(raw)
    os.chmod(dst, 0o600)
    prune(name)
    print(f"  {name}: snapshot -> env-backups/{dst.name} ({len(snapshots_for(name))} kept)")
    return False


def main() -> None:
    load_env()
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)

    if "--test" in sys.argv:
        ok = send("🟢 env-backup watchdog liveness check — ignore.")
        print("test DM sent" if ok else "test DM failed")
        return

    any_bad = False
    for rel in TARGETS:
        target = APP / rel
        if not target.exists():
            continue
        try:
            if process(target):
                any_bad = True
        except Exception as e:  # noqa: BLE001 — never let one file abort the other
            print(f"  {rel}: error {type(e).__name__}: {e}", file=sys.stderr)
    # Non-zero exit on a bad write makes it show in `systemctl status` too.
    sys.exit(1 if any_bad else 0)


if __name__ == "__main__":
    main()
