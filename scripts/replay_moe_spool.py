#!/usr/bin/env python3
"""Replay MOE rows spooled after Google Sheets append failures."""

from __future__ import annotations

import json
import fcntl
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from moe import (
    MOE_PENDING_LOCK_PATH,
    MOE_PENDING_PATH,
    configured_opinion_store,
)


def main() -> None:
    MOE_PENDING_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(
        MOE_PENDING_LOCK_PATH,
        os.O_WRONLY | os.O_CREAT,
        0o600,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lock_fd)
        raise RuntimeError("Another MOE spool replay is already running") from exc
    try:
        _replay()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _replay() -> None:
    replay_path = MOE_PENDING_PATH.with_suffix(".jsonl.replay")
    if not replay_path.exists() and MOE_PENDING_PATH.exists():
        os.replace(MOE_PENDING_PATH, replay_path)
    if not replay_path.exists():
        print("No pending MOE rows.")
        return
    entries = [
        json.loads(line)
        for line in replay_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    store = configured_opinion_store()
    remaining = []
    replayed = 0
    for entry in entries:
        try:
            store.append(entry["row"])
            replayed += 1
        except Exception as exc:
            print(
                f"Failed to replay {entry['row'].get('opinion_id')}: {exc}",
                file=sys.stderr,
            )
            remaining.append(entry)
    if remaining:
        fd = os.open(
            MOE_PENDING_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            for item in remaining:
                line = (
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    replay_path.unlink()
    print(f"Replayed {replayed}; {len(remaining)} still pending.")


if __name__ == "__main__":
    main()
