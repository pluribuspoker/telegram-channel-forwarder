#!/usr/bin/env python3
"""
Regression test for _insert_odds line placement.

For each recent channel message that has an odds tag:
  1. Strip the tag to reconstruct the "original" message
  2. AI-parse to get picks
  3. Run BOTH old (main) and new (patched) _insert_odds with dummy odds
  4. PASS if both produce the same tag placement
  5. DIFF if they differ — may be a fix or a regression (printed for review)

Usage:
  python scripts/test_insert_odds_regression.py
"""
import asyncio, os, re, sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)
load_dotenv(ROOT / ".env.local", override=True)

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.extensions import html as tl_html

import importlib.util

from ai import claude_parse

# Old version: from main branch (on sys.path via ROOT)
from tracker import _insert_odds as _insert_odds_old, _ODDS_TAG_RE

# New version: patched tracker in this worktree
_spec = importlib.util.spec_from_file_location("tracker_new", ROOT / "tracker.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_insert_odds_new = _mod._insert_odds

CHANNEL_ID = int(os.environ.get("DEST_CHANNEL", "-1002486251914"))


def find_tagged_lines(text: str) -> list[int]:
    return [i for i, l in enumerate(text.split("\n")) if _ODDS_TAG_RE.search(l)]


def strip_odds_tags(text: str) -> str:
    return "\n".join(_ODDS_TAG_RE.sub("", l) for l in text.split("\n"))


async def main():
    client = TelegramClient(
        StringSession(os.environ["TELEGRAM_SESSION"]),
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
    )
    await client.start()

    msgs = await client.get_messages(CHANNEL_ID, limit=300)
    tagged = []
    for msg in msgs:
        if not msg.text:
            continue
        ht = tl_html.unparse(msg.text, msg.entities or [])
        if find_tagged_lines(ht):
            tagged.append((msg.id, ht))

    await client.disconnect()

    print(f"Testing {len(tagged)} messages with existing odds tags\n")

    passed = diffs = skipped = 0

    for msg_id, current_text in tagged:
        original_text = strip_odds_tags(current_text)

        parsed = await claude_parse(original_text)
        if not parsed:
            print(f"  msg {msg_id}: SKIP (parse failed)")
            skipped += 1
            continue

        picks = parsed.get("picks", [])
        if not picks:
            print(f"  msg {msg_id}: SKIP (no picks extracted)")
            skipped += 1
            continue

        dummy = {str(i): {"odds": -110, "match_type": ""} for i in range(len(picks))}

        old_result = _insert_odds_old(original_text, picks, dummy)
        new_result = _insert_odds_new(original_text, picks, dummy)

        old_lines = find_tagged_lines(old_result)
        new_lines = find_tagged_lines(new_result)

        if old_result == new_result:
            print(f"  msg {msg_id}: PASS  tag(s) on line(s) {new_lines}")
            passed += 1
        else:
            print(f"  msg {msg_id}: DIFF  old→{old_lines}  new→{new_lines}")
            orig_lines = original_text.split("\n")
            for i, l in enumerate(orig_lines[:14]):
                old_m  = " <OLD>" if i in old_lines and i not in new_lines else ""
                new_m  = " <NEW>" if i in new_lines and i not in old_lines else ""
                both_m = " <BOTH>" if i in old_lines and i in new_lines else ""
                if old_m or new_m or both_m:
                    print(f"    {i:2}: {l[:90].encode('ascii','replace').decode()}{old_m}{new_m}{both_m}")
            diffs += 1

    print(f"\nResults: {passed} same, {diffs} differ, {skipped} skipped")
    if diffs:
        print("(DIFF lines above need manual review — could be fixes or regressions)")


if __name__ == "__main__":
    asyncio.run(main())
