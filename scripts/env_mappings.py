#!/usr/bin/env python3
"""env_mappings.py — list / add / remove MAPPINGS_CONFIG entries safely.

MAPPINGS_CONFIG is one single-quoted JSON line in .env whose backslashes are
doubled (the "four backslashes" rule: `\\d+` in a regex is `\\\\d+` on disk),
which makes it the easiest value in the file to break by hand. This edits the
parsed list and writes it back through set_env_local.py — the only write the
VPS .env guard (deploy/env_backup.py) keeps — after proving the new line
round-trips through python-dotenv to exactly the intended list.

Usage:
    python3 scripts/env_mappings.py list
    python3 scripts/env_mappings.py add '{"id":"x-to-y","source_channel":-100…,"dest_channel":-100…}'
    python3 scripts/env_mappings.py remove dfav-to-df
    python3 scripts/env_mappings.py --file .env list     # default file: .env at repo root
"""

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
KEY = "MAPPINGS_CONFIG"


def encode(mappings: list) -> str:
    """The on-disk value (without the key) for `mappings`."""
    s = json.dumps(mappings, separators=(",", ":"), ensure_ascii=False)
    if "'" in s:
        raise ValueError("a mapping contains a single quote — can't live in a single-quoted .env value")
    return "'" + s.replace("\\", "\\\\") + "'"


def decode(raw_value: str) -> list:
    return json.loads(dotenv_values(stream=io.StringIO(f"{KEY}={raw_value}\n"))[KEY])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(ROOT / ".env"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("mapping", help="one mapping as a JSON object (needs a unique id)")
    r = sub.add_parser("remove")
    r.add_argument("id")
    args = ap.parse_args()

    mappings = json.loads(dotenv_values(args.file)[KEY])
    ids = [m.get("id") for m in mappings]

    if args.cmd == "list":
        for m in mappings:
            print(f"{m.get('id', '?'):<16} {m.get('source_channel')} → {m.get('dest_channel')}"
                  + (" [no_broadcast]" if m.get("no_broadcast") else ""))
        return 0

    if args.cmd == "add":
        m = json.loads(args.mapping)
        if not isinstance(m, dict) or not m.get("id") or not m.get("source_channel") or not m.get("dest_channel"):
            ap.error("mapping must be a JSON object with id, source_channel and dest_channel")
        if m["id"] in ids:
            ap.error(f"id {m['id']!r} already exists — remove it first")
        new = mappings + [m]
    else:
        if args.id not in ids:
            ap.error(f"no mapping with id {args.id!r} (have: {', '.join(map(str, ids))})")
        new = [m for m in mappings if m.get("id") != args.id]

    raw = encode(new)
    if decode(raw) != new:
        print("ABORT: encoded value does not round-trip through dotenv", file=sys.stderr)
        return 1
    rc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "set_env_local.py"), "--file", args.file, "--stdin", KEY],
        input=raw, text=True,
    ).returncode
    if rc == 0:
        print(f"{args.cmd} ok — {len(new)} mappings. Restart telegram-forwarder to apply "
              f"(grade-daemon too if broadcast/no_broadcast changed).")
    return rc


if __name__ == "__main__":
    sys.exit(main())
