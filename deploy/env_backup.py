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

VPS IS THE SOURCE OF TRUTH FOR .env (2026-09-28). `syncenv` used to push the
desktop copy over the server's, and a stale desktop copy silently deleted keys
(X cookies, 2026-07-19) or resurrected removed mappings (Tony POD 2026-09-19,
dfav 2026-09-28). Now any write to .env that did not come through
scripts/set_env_local.py (which snapshots its result BEFORE replacing the file,
so its writes read as "unchanged" here) is RECONCILED against the newest
snapshot: the server's lines are kept byte-for-byte, keys that are genuinely
NEW are accepted (appended — "add locally first, then syncenv" still works),
and changed values, dropped keys, and re-added tombstoned keys (removed on
purpose via `set_env_local.py --unset`) are rejected with one DM. Change a
server value with set_env_local.py (or env_mappings.py), never by push.
.env.local is not synced and keeps the validate-and-alert behaviour only.

Silent unless something is wrong (same contract as the other watchdogs).
Reuses WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID. Stdlib + optional telethon.

Usage:
  python deploy/env_backup.py            # one cycle (path unit + timer call this)
  python deploy/env_backup.py --test     # send a liveness DM and exit
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
BACKUP_DIR = Path(os.environ.get("ENV_BACKUP_DIR") or Path.home() / "env-backups")
TARGETS = [".env", ".env.local"]
GUARDED = {".env"}  # VPS-wins reconcile; .env.local is never synced
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


def snapshots_for(name: str, backup_dir: Path | None = None):
    """Existing good snapshots for `name`, oldest-first.

    Exact match on the timestamp suffix, NOT a glob — glob(".env.*.bak") also
    matches ".env.local.*.bak", which made .env validate against .env.local's
    keyset and false-alarm on every run.
    """
    pat = re.compile(re.escape(name) + r"\.\d{8}_\d{6}\.bak$")
    return sorted(p for p in (backup_dir or BACKUP_DIR).iterdir() if pat.match(p.name))


def tombstones_path(name: str, backup_dir: Path | None = None) -> Path:
    return (backup_dir or BACKUP_DIR) / f"{name}.tombstones.json"


def load_tombstones(name: str, backup_dir: Path | None = None) -> set:
    try:
        return set(json.loads(tombstones_path(name, backup_dir).read_text()))
    except (OSError, ValueError):
        return set()


def save_tombstones(name: str, keys: set, backup_dir: Path | None = None) -> None:
    _write_private(tombstones_path(name, backup_dir), json.dumps(sorted(keys)).encode())


def _write_private(path: Path, raw: bytes) -> None:
    """Atomic write, mode 600, owned by forwarder even when run as root."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        if os.geteuid() == 0:
            import pwd
            pw = pwd.getpwnam("forwarder")
            os.chown(tmp, pw.pw_uid, pw.pw_gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_snapshot(name: str, raw: bytes, backup_dir: Path | None = None) -> Path:
    """Write `raw` as the newest good snapshot of `name` and prune.

    set_env_local.py calls this BEFORE it replaces the file, which is what
    marks its write as sanctioned: the watcher then finds the file equal to
    the newest snapshot and leaves it alone.
    """
    d = backup_dir or BACKUP_DIR
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = d / f"{name}.{ts}.bak"
    _write_private(dst, raw)
    prune(name, d)
    return dst


def reconcile(pushed: str, baseline: str, tombstones: set = frozenset()):
    """VPS-wins merge of a foreign write over the newest snapshot.

    Returns (merged_text, added, changed, dropped, resurrected): the baseline
    kept byte-for-byte plus the raw lines of keys that are new to it; every
    other difference is rejected and only reported.
    """
    new_env, old_env = parse_env(pushed), parse_env(baseline)
    added = [k for k in new_env if k not in old_env and k not in tombstones]
    resurrected = sorted(k for k in new_env if k not in old_env and k in tombstones)
    changed = sorted(k for k in new_env if k in old_env and new_env[k] != old_env[k])
    dropped = sorted(set(old_env) - set(new_env))
    merged = baseline
    if added:
        if merged and not merged.endswith("\n"):
            merged += "\n"
        last_line = {}
        for line in pushed.splitlines():
            k = line.partition("=")[0].strip()
            if k in added and "=" in line and not line.strip().startswith("#"):
                last_line[k] = line
        merged += "".join(last_line[k] + "\n" for k in added)
    return merged, added, changed, dropped, resurrected


def wait_stable(path: Path, quiet: float = 1.5, limit: float = 30.0) -> bytes:
    """Read `path` once it stops changing — scp writes in place, and the path
    unit fires on the first write, so reading at once can see half a file."""
    deadline = time.monotonic() + limit
    prev = None
    while True:
        st = path.stat()
        cur = (st.st_mtime_ns, st.st_size)
        if cur == prev or time.monotonic() > deadline:
            return path.read_bytes()
        prev = cur
        time.sleep(quiet)


def guard(target: Path, raw: bytes) -> bool:
    """VPS-wins reconcile of a foreign write to a GUARDED file.

    Returns True when something was rejected (DM sent). A no-op (False) when
    the file equals the newest snapshot or there is no snapshot to defend.
    """
    name = target.name
    snaps = snapshots_for(name)
    if not snaps:
        return False
    base_raw = snaps[-1].read_bytes()
    if sha(base_raw) == sha(raw):
        return False
    pushed = raw.decode(errors="replace")
    merged, added, changed, dropped, resurrected = reconcile(
        pushed, base_raw.decode(errors="replace"), load_tombstones(name))
    bad_sessions = [k for k in added if validate_session_value(k, parse_env(pushed)[k])]
    if bad_sessions:
        merged, added, *_ = reconcile(
            pushed, base_raw.decode(errors="replace"),
            load_tombstones(name) | set(bad_sessions))
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    _write_private(BACKUP_DIR / f"{name}.{ts}.pushed", raw)  # forensic copy
    merged_raw = merged.encode()
    if merged_raw != base_raw:
        record_snapshot(name, merged_raw)
    _write_private(target, merged_raw)
    print(f"  {name}: foreign write reconciled (VPS wins) — added {added}, "
          f"rejected changed {changed}, dropped {dropped}, tombstoned {resurrected}, "
          f"bad sessions {bad_sessions}")
    rejected = changed or dropped or resurrected or bad_sessions
    if rejected:
        lines = [f"🛡️ {name} on the VPS was overwritten (syncenv?) — the VPS copy wins, restored."]
        if added:
            lines.append("Accepted (new keys): " + ", ".join(added))
        if changed:
            lines.append("Ignored — different value on the VPS: " + ", ".join(changed))
        if dropped:
            lines.append("Ignored — missing from the pushed copy: " + ", ".join(dropped))
        if resurrected:
            lines.append("Ignored — deliberately removed on the VPS: " + ", ".join(resurrected))
        if bad_sessions:
            lines.append("Ignored — session won't parse: " + ", ".join(bad_sessions))
        lines.append("Change a server value with scripts/set_env_local.py --file .env "
                     "(mappings: scripts/env_mappings.py); refresh the desktop copy "
                     f"with scripts/pull_env.py. Pushed copy: env-backups/{name}.{ts}.pushed")
        send("\n".join(lines))
    return bool(rejected)


def validate_session_value(key: str, value: str) -> bool:
    """True when `key` is a *_SESSION whose value won't parse."""
    if not key.endswith(SESSION_SUFFIX):
        return False
    try:
        from telethon.sessions import StringSession
    except ImportError:
        return False
    try:
        StringSession(value)
    except Exception:  # noqa: BLE001
        return True
    return False


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


def prune(name: str, backup_dir: Path | None = None) -> None:
    d = backup_dir or BACKUP_DIR
    pushed = sorted(p for p in d.iterdir()
                    if re.fullmatch(re.escape(name) + r"\.\d{8}_\d{6}\.pushed", p.name))
    snaps = snapshots_for(name, backup_dir)
    for old in snaps[:-KEEP] + pushed[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def process(target: Path) -> bool:
    """Handle one file. Return True if a bad write was detected."""
    name = target.name
    raw = wait_stable(target)
    if name in GUARDED:
        # A reconciled push is handled, not a failure: its DM is the signal,
        # and a non-zero exit would page healthchecks + spawn an hc-repair agent.
        guard(target, raw)
        raw = target.read_bytes()
    return process_validated(target, raw)


def process_validated(target: Path, raw: bytes) -> bool:
    name = target.name
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
    dst = record_snapshot(name, raw)
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
