#!/usr/bin/env python3
"""
Corpus replay for _insert_odds tag placement: old (a git ref) vs new (working tree).

For every priced parse_cache.json entry with rendered html_text:
  1. strip the odds tags to reconstruct the untagged post
  2. run _insert_odds from BOTH tracker_format versions with the entry's
     real picks + odds_by_pick
  3. SAME if the outputs match; DIFF otherwise (a fix or a regression —
     printed for review, exit 1)

Offline: no Telegram, no Claude. This is the "corpus replay" docs/odds.md
names as the real net for placement changes.

The old version of this script read 300 live posts through the operator's
Telethon session, re-parsed each through Claude, and imported `_ODDS_TAG_RE`
from tracker — which left tracker in the 2026-04-03 module split (92b29dc),
so it had crashed on import ever since.

Usage:
  python scripts/test_insert_odds_regression.py              # vs HEAD
  python scripts/test_insert_odds_regression.py --base origin/main~3
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tracker_format as new_tf  # noqa: E402
from tracker_format import _ODDS_TAG_RE  # noqa: E402


def load_old(ref: str):
    src = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:tracker_format.py"],
                         check=True, capture_output=True, text=True).stdout
    path = Path(tempfile.mkdtemp()) / "tracker_format_old.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("tracker_format_old", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # its own imports resolve against ROOT (sys.path)
    return mod


def tagged_lines(text: str) -> list[int]:
    return [i for i, l in enumerate(text.split("\n")) if _ODDS_TAG_RE.search(l)]


def strip_odds_tags(text: str) -> str:
    return "\n".join(_ODDS_TAG_RE.sub("", l) for l in text.split("\n"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="HEAD", help="git ref for the OLD tracker_format.py")
    ap.add_argument("--cache", default=str(ROOT / "parse_cache.json"))
    args = ap.parse_args()

    old_tf = load_old(args.base)
    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    same = diffs = errors = 0
    for key, e in cache.items():
        if not isinstance(e, dict) or e.get("_dupe") or not e.get("html_text"):
            continue
        picks = (e.get("parsed") or {}).get("picks") or []
        odds = e.get("odds_by_pick") or {}
        if not picks or not any(isinstance(o, dict) and o.get("odds") is not None
                                for o in odds.values()):
            continue
        original = strip_odds_tags(e["html_text"])
        try:
            old = old_tf._insert_odds(original, picks, odds)
            new = new_tf._insert_odds(original, picks, odds)
        except Exception as exc:  # noqa: BLE001 - report, keep replaying
            print(f"  {key}: ERROR {type(exc).__name__}: {exc}")
            errors += 1
            continue
        if old == new:
            same += 1
            continue
        diffs += 1
        ol, nl = tagged_lines(old), tagged_lines(new)
        print(f"  {key}: DIFF  old→{ol}  new→{nl}")
        for i, (a, b) in enumerate(zip(old.split("\n"), new.split("\n"))):
            if a != b:
                print(f"    {i:2} old: {a[:100]}\n    {i:2} new: {b[:100]}")

    print(f"\nReplayed vs {args.base}: {same} same, {diffs} differ, {errors} errors")
    if diffs:
        print("(DIFF lines above need review — could be fixes or regressions)")
    return 1 if diffs or errors else 0


if __name__ == "__main__":
    sys.exit(main())
