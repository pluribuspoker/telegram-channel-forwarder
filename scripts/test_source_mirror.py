"""Regression test: `"grade_source": true` mirrors odds + result emojis onto the SOURCE post.

Self-contained (no Telegram — a fake client records edits; state goes to a temp file):

    ~/venv/bin/python scripts/test_source_mirror.py

DAGGER → Fight Club Picks (2026-09-29): the operator reads their own source
channel and expects the ✅ the FC copy got. Pins:

  1. a resolved pick on a flagged mapping edits the source (emoji on the pick line)
  2. a pending pick with odds gets the odds tag; the result arrives on a later pass
  3. an unchanged signature fetches nothing; a re-render of a marked post is a no-op
  4. a dest `_dupe` marker mirrors from its primary entry
  5. unflagged mappings, stale posts, and dry state never touch the source
  6. a failed edit doesn't advance state (retried next run)
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import source_mirror  # noqa: E402

DEST = -1002486251914
SRC = "3736599319"
TEXT = "Dagger\n\n❗️ ❗️  MAIN PLAY ❗️ ❗️ \n \nEagles / Bears UNDER 42.5 -110 2U"
PICK = {"description": "Eagles vs Bears UNDER 42.5", "sport": "NFL", "bet_type": "total",
        "is_parlay_leg": False, "period": "game", "teams": ["Philadelphia Eagles", "Chicago Bears"],
        "player": None, "prop_stat": None, "line": 42.5, "direction": "under"}
TODAY = date(2026, 9, 29)
# Pin the mirror's "today" to the fixtures' week: against the real clock the
# MIRROR_DAYS cutoff aged every 2026-09-28 fixture out on 2026-10-06 and the
# plan came back empty (8 FAILs, no edits).
_real_plan = source_mirror.plan_source_syncs
source_mirror.plan_source_syncs = lambda *a, **kw: _real_plan(*a, **{**kw, "today": TODAY})


def entry(verdict=None, odds=-112, mapping="dagger-to-fc", msg_date="2026-09-28", **kw):
    e = {"capper_name": "Dagger", "parsed": {"sport": "NFL", "picks": [PICK]},
         "leg_verdicts": {"0": {"verdict": verdict, "calc": "", "sport": "NFL"}} if verdict else {},
         "odds_by_pick": {"0": {"odds": odds, "match_type": "exact"}},
         "mapping_id": mapping, "_source_key": f"{SRC}:2", "msg_date": msg_date}
    e.update(kw)
    return e


class _Msg:
    def __init__(self, mid, text):
        self.id, self.raw_text, self.entities = mid, text, []


class _Client:
    def __init__(self, text=TEXT, fail_edit=False):
        self.text, self.fail_edit = text, fail_edit
        self.fetches, self.edits = 0, []

    async def get_messages(self, chat, ids):
        self.fetches += 1
        return _Msg(ids, self.text)

    async def edit_message(self, chat, mid, text, parse_mode=None):
        if self.fail_edit:
            raise RuntimeError("CHAT_WRITE_FORBIDDEN")
        self.edits.append((chat, mid, text))
        self.text = text      # the post now carries it (plain text: no entities here)


def run(cache, client, ids=frozenset({"dagger-to-fc"})):
    source_mirror.grade_source_mapping_ids = lambda: ids
    return asyncio.run(source_mirror.sync_source_mirrors(client, cache))


def main():
    tmp = tempfile.mkdtemp()
    source_mirror.STATE_PATH = os.path.join(tmp, "state.json")
    fails = 0

    def reset():
        if os.path.exists(source_mirror.STATE_PATH):
            os.remove(source_mirror.STATE_PATH)

    def check(name, cond):
        nonlocal fails
        print(("PASS " if cond else "FAIL ") + name)
        fails += not cond

    # 1+2: pending with a stated price → no tag (src_declined), nothing to edit yet
    c = _Client()
    run({f"{DEST}:3922": entry()}, c)
    check("stated price: pending pick leaves the source untouched", c.edits == [])
    # result arrives → ✅ on the pick line
    run({f"{DEST}:3922": entry("WIN")}, c)
    check("WIN edits the source once", len(c.edits) == 1)
    check("emoji lands on the pick line", c.edits and c.edits[0][2].endswith("UNDER 42.5 -110 2U✅"))
    check("edit targets the -100 source chat + source msg id",
          c.edits and c.edits[0][:2] == (int(f"-100{SRC}"), 2))

    # 3: unchanged signature → no fetch at all
    before = c.fetches
    run({f"{DEST}:3922": entry("WIN")}, c)
    check("unchanged signature fetches nothing", c.fetches == before)
    # forced re-render over the marked post is a no-op
    marked = c.text
    check("re-render of a marked post is idempotent",
          source_mirror.render_source(marked, entry("WIN")) == marked)

    # odds tag on an unpriced post
    reset()
    c2 = _Client(text="Dagger\n\nEagles / Bears UNDER 42.5 2U")
    run({f"{DEST}:3922": entry()}, c2)
    check("unpriced pending pick gets the odds tag",
          c2.edits and "[-112]" in c2.edits[0][2] and "✅" not in c2.edits[0][2])
    run({f"{DEST}:3922": entry("LOSS")}, c2)
    check("later result adds the emoji after the tag",
          len(c2.edits) == 2 and c2.edits[1][2].count("[-112]") == 1 and "❌" in c2.edits[1][2])

    # 4: dupe marker follows its primary
    reset()
    c3 = _Client()
    primary = entry("WIN")
    primary.pop("_source_key")      # posted by hand in FC: no provenance of its own
    run({f"{DEST}:3921": primary,
         f"{DEST}:3922": {"_dupe": True, "primary_id": 3921, "mapping_id": "dagger-to-fc",
                          "_source_key": f"{SRC}:2"}}, c3)
    check("dupe marker mirrors its primary's verdict",
          c3.edits and c3.edits[0][2].endswith("2U✅"))

    # 5: unflagged / stale / nothing-yet
    reset()
    c4 = _Client()
    run({f"{DEST}:1": entry("WIN", mapping="cicl-to-fc")}, c4)
    run({f"{DEST}:2": entry("WIN", msg_date="2026-09-01")}, c4)
    run({f"{DEST}:3": entry(odds=None)}, c4)
    run({f"{DEST}:4": entry("WIN")}, c4, ids=frozenset())
    check("unflagged / stale / empty / no flags: zero fetches", c4.fetches == 0)

    # 6: failed edit keeps the state behind
    reset()
    c5 = _Client(fail_edit=True)
    run({f"{DEST}:3922": entry("WIN")}, c5)
    run({f"{DEST}:3922": entry("WIN")}, c5)
    check("failed edit retries next run", c5.fetches == 2)

    # MAPPINGS_CONFIG parsing (restore the real reader)
    import importlib
    importlib.reload(source_mirror)
    os.environ["MAPPINGS_CONFIG"] = json.dumps([
        {"id": "a", "grade_source": True}, {"id": "b"}, {"grade_source": True}])
    check("grade_source_mapping_ids reads flagged ids only",
          source_mirror.grade_source_mapping_ids() == frozenset({"a"}))

    print(f"\n{'OK' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
