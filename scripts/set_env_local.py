#!/usr/bin/env python3
"""set_env_local.py — safely add/update keys in .env.local without clobbering it.

Reads the EXISTING .env.local, updates or appends ONLY the given KEY=VALUE
pairs (leaving every other key, comment, and blank line untouched), validates
the result, and writes it back atomically (mode 600, owner forwarder).

It never rewrites the file from scratch. This is the guardrail against the
2026-09-05 incident, where a hand-edit that meant to add one key rewrote the
whole file — dropping BOT_SESSION / X_AUTH_TOKEN / X_CT0 / PIKKIT_TOKEN and
corrupting TELEGRAM_SESSION into unparseable garbage — and silently took the
pick grader down for 8 hours. Add keys with this, never by hand.

Usage:
    python3 scripts/set_env_local.py TELEGRAM_SESSION=1Abc...  AK_TELEGRAM_USER_ID=123
    python3 scripts/set_env_local.py --file .env.local KEY=VALUE
    python3 scripts/set_env_local.py --validate            # check the file, change nothing

Guarantees:
    * No key that was present before can be dropped (superset invariant).
    * Any *_SESSION value is StringSession-parseable before it is written.
    * A value may not contain a newline (that is exactly how the session got
      mangled last time).
    * The write is atomic (temp file + os.replace), so a crash mid-write can
      never leave a half-written .env.local.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION_SUFFIX = "_SESSION"


def parse_line(line: str):
    """Return (key, value) for a KEY=VALUE line, else None (comment/blank/cont)."""
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    k, _, v = line.partition("=")
    return k.strip(), v.rstrip("\n")


def existing_keys(lines) -> set:
    keys = set()
    for line in lines:
        kv = parse_line(line)
        if kv:
            keys.add(kv[0])
    return keys


def validate_session(key: str, value: str) -> None:
    """Raise ValueError if a *_SESSION value won't parse. No-op without telethon."""
    if not key.endswith(SESSION_SUFFIX):
        return
    try:
        from telethon.sessions import StringSession
    except ImportError:
        print(f"  (telethon unavailable — skipped StringSession check for {key})",
              file=sys.stderr)
        return
    try:
        StringSession(value)
    except Exception as e:  # noqa: BLE001 — telethon raises struct.error, ValueError, ...
        raise ValueError(
            f"{key} is not a valid Telethon StringSession "
            f"({type(e).__name__}: {e}); refusing to write it"
        )


def apply_updates(lines, updates: dict):
    """Return new list of lines with `updates` merged in (replace-in-place or append)."""
    out = []
    seen = set()
    for line in lines:
        kv = parse_line(line)
        if kv and kv[0] in updates:
            out.append(f"{kv[0]}={updates[kv[0]]}\n")
            seen.add(kv[0])
        else:
            out.append(line if line.endswith("\n") else line + "\n")
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}\n")
    return out


def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.local.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        if os.geteuid() == 0:  # running as root — keep it owned by forwarder
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


def summarize(lines) -> str:
    parts = []
    for line in lines:
        kv = parse_line(line)
        if kv:
            parts.append(f"{kv[0]}(len={len(kv[1])})")
    return ", ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(ROOT / ".env.local"),
                    help="target env file (default: .env.local at repo root)")
    ap.add_argument("--validate", action="store_true",
                    help="validate the existing file and exit; make no changes")
    ap.add_argument("pairs", nargs="*", metavar="KEY=VALUE",
                    help="keys to add or update")
    args = ap.parse_args()

    path = Path(args.file)
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    before = existing_keys(lines)

    if args.validate:
        ok = True
        for line in lines:
            kv = parse_line(line)
            if not kv:
                continue
            try:
                validate_session(*kv)
            except ValueError as e:
                print(f"  INVALID: {e}", file=sys.stderr)
                ok = False
        print(f"keys: {summarize(lines)}")
        print("VALID" if ok else "INVALID")
        return 0 if ok else 1

    if not args.pairs:
        ap.error("give at least one KEY=VALUE (or --validate)")

    updates = {}
    for p in args.pairs:
        if "=" not in p:
            ap.error(f"not a KEY=VALUE pair: {p!r}")
        k, _, v = p.partition("=")
        k = k.strip()
        if "\n" in v or "\r" in v:
            ap.error(f"value for {k} contains a newline — refusing (this is how "
                     f"the session got corrupted before)")
        try:
            validate_session(k, v)  # checked before we touch the file
        except ValueError as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return 1
        updates[k] = v

    new_lines = apply_updates(lines, updates)

    after = existing_keys(new_lines)
    dropped = before - after
    if dropped:  # must never happen by construction — belt and suspenders
        print(f"ABORT: would drop keys {sorted(dropped)}", file=sys.stderr)
        return 1

    atomic_write(path, "".join(new_lines))
    print(f"updated {path} — set {sorted(updates)}")
    print(f"keys now: {summarize(new_lines)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
