"""Regression test: a mapping with "no_broadcast": true never posts results.

Self-contained (no Telegram/API — nothing is sent, the cache save is stubbed):

    ~/venv/bin/python scripts/test_no_broadcast_mapping.py

Broadcast targets are keyed per DEST channel, so DAGGER → Fight Club Picks
(2026-09-28: odds + result emojis, no broadcasts) shares FC's results channel
with the CI mappings. The opt-out is per message via the entry's `mapping_id`:

  1. a muted mapping's result is marked broadcasted (terminal) but not posted
  2. a sibling result on the same dest + game from a broadcasting mapping
     still posts, alone
  3. _build_no_broadcast_ids reads only flagged mappings with an id
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import grade_daemon  # noqa: E402

DEST, BC = -1002486251914, -1002497575464


class _Audit:
    def __init__(self):
        self.broadcast_results_mappings = {DEST: BC}
        self.sent = []

    async def broadcast_results(self, **kw):
        self.sent.append(("single", kw["message_id"]))

    async def broadcast_group(self, **kw):
        self.sent.append(("group", tuple(i["message_id"] for i in kw["items"])))


def _item(mid):
    return {
        "cache_key": f"{DEST}:{mid}", "channel_id": DEST, "message_id": mid,
        "capper": "X", "reply_to_id": None,
        "bc_results": [({"description": "Eagles/Bears U42.5"}, "WIN", -110)],
        "sheets_results": [], "leg_indices": [0], "mark_all_resolved": False,
        "parlay_legs": [], "html_text": "", "msg_date": "2026-09-28",
        "game_key": None, "matchup": [], "event": None, "leg_games": [],
    }


async def _run():
    cache = {
        f"{DEST}:1": {"mapping_id": "dagger-to-fc", "leg_verdicts": {"0": {"verdict": "WIN"}}},
        f"{DEST}:2": {"mapping_id": "cicl-to-fc", "leg_verdicts": {"0": {"verdict": "WIN"}}},
    }

    async def _no_keys(pending, espn_cache):
        for it in pending:
            it["game_key"] = ("NFL", "2026-09-28", ("bears", "eagles"))

    grade_daemon._resolve_game_keys = _no_keys
    grade_daemon._save_pending_cache = lambda c: None
    audit = _Audit()
    await grade_daemon._flush_broadcasts(
        [_item(1), _item(2)], audit, cache, {}, None,
        frozenset({"dagger-to-fc"}),
    )
    assert cache[f"{DEST}:1"]["leg_verdicts"]["0"].get("broadcasted") is True, "muted leg not marked"
    assert cache[f"{DEST}:2"]["leg_verdicts"]["0"].get("broadcasted") is True
    assert all(1 not in (s[1] if isinstance(s[1], tuple) else (s[1],)) for s in audit.sent), audit.sent
    assert audit.sent == [("single", 2)], audit.sent
    print("PASS 1-2 muted mapping marked, not posted; sibling posts alone")


def main():
    asyncio.run(_run())
    os.environ["MAPPINGS_CONFIG"] = json.dumps([
        {"id": "a", "no_broadcast": True}, {"id": "b"}, {"no_broadcast": True},
        {"id": "c", "no_broadcast": False},
    ])
    assert grade_daemon._build_no_broadcast_ids() == frozenset({"a"})
    print("PASS 3 _build_no_broadcast_ids")


if __name__ == "__main__":
    main()
