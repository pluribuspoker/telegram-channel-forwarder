#!/usr/bin/env python3
"""pull_env.py — refresh the desktop .env FROM the VPS (VPS = source of truth).

Replaces the old push-direction `syncenv` (local .env → server), which let a
stale desktop copy delete server keys and resurrect removed mappings. The
server now reconciles any pushed .env back to its own copy anyway
(deploy/env_backup.py), so pushing gains nothing; pull instead:

    python scripts/pull_env.py            # show the key diff, then overwrite local .env
    python scripts/pull_env.py --dry-run  # diff only

Point the desktop `syncenv` alias here. The local copy is kept as
.env.pre-pull.<timestamp> before it is replaced. Change server values on the
VPS with scripts/set_env_local.py --file .env / scripts/env_mappings.py.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMOTE = "root@209.38.51.86:/home/forwarder/app/.env"


def parse_env(text: str) -> dict:
    d = {}
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, _, v = line.partition("=")
            d[k.strip()] = v
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--remote", default=REMOTE)
    args = ap.parse_args()

    local = ROOT / ".env"
    fd, tmp = tempfile.mkstemp(prefix="env-pull-")
    os.close(fd)
    try:
        if subprocess.run(["scp", "-q", args.remote, tmp]).returncode != 0:
            print("scp failed — local .env untouched", file=sys.stderr)
            return 1
        remote_text = Path(tmp).read_text(encoding="utf-8")
        new = parse_env(remote_text)
        if not new:
            print("remote .env parsed empty — refusing to overwrite", file=sys.stderr)
            return 1
        old = parse_env(local.read_text(encoding="utf-8")) if local.exists() else {}
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
        for label, keys in (("+ server has", added), ("- only local (dropped)", removed),
                            ("~ server value wins", changed)):
            if keys:
                print(f"{label}: {', '.join(keys)}")
        if not (added or removed or changed):
            print("local .env already matches the VPS")
            return 0
        if args.dry_run:
            return 0
        if local.exists():
            keep = ROOT / f".env.pre-pull.{datetime.now():%Y%m%d_%H%M%S}"
            shutil.copy2(local, keep)
            print(f"previous local copy → {keep.name}")
        shutil.copyfile(tmp, local)
        print("local .env ← VPS")
        return 0
    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    sys.exit(main())
