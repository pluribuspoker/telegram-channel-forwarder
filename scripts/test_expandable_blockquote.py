"""Regression test: edits must not expand a collapsed (expandable) blockquote.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_expandable_blockquote.py

Guards the bug where the odds edit on -1002486251914:3651 expanded the
source's collapsed quote: stock Telethon renders every blockquote as a plain
<blockquote> (ENTITY_TO_FORMATTER ignores `collapsed`), so the Bot API edit
built from to_bot_html re-created the quote expanded — and since later
tracker/daemon rebuilds read the now-expanded live entities, the flag was
gone for good. Telethon's HTML *parser* has the mirror gap, which would do
the same to Telethon edits in send_as_user channels (parse_mode="html").

Pins patch_expandable_blockquotes() (tracker_format): unparse emits
<blockquote expandable> for collapsed quotes, parse restores collapsed=True
from it, and odds/emoji placement still treats the expandable quote as a
blockquote (never the bet line).

Fixture is the byte-exact source/dest snapshot of the incident message.
"""
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker_format import (
    _blockquote_lines,
    _insert_emojis,
    _insert_odds,
    patch_expandable_blockquotes,
    to_bot_html,
)

FIXTURE = json.load(open(Path(__file__).resolve().parent / "fixtures" / "expandable_blockquote.json", encoding="utf-8"))


def _entities(spec):
    from telethon.tl.types import MessageEntityBlockquote
    ents = []
    for e in spec:
        assert e["type"] == "MessageEntityBlockquote", e
        ents.append(MessageEntityBlockquote(offset=e["offset"], length=e["length"], collapsed=e["collapsed"]))
    return ents


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    src = FIXTURE["source"]
    src_ents = _entities(src["entities"])

    # 1. unparse: collapsed quote renders as <blockquote expandable>
    ht = to_bot_html(src["raw_text"], src_ents)
    check("collapsed → <blockquote expandable>", "<blockquote expandable>" in ht, ht[:120])
    check("no bare <blockquote> for collapsed quote", "<blockquote>" not in ht)

    # 2. unparse: a non-collapsed quote still renders bare (no attribute)
    plain_ents = copy.deepcopy(src_ents)
    plain_ents[0].collapsed = False
    ht_plain = to_bot_html(src["raw_text"], plain_ents)
    check("non-collapsed → plain <blockquote>", "<blockquote>" in ht_plain and "expandable" not in ht_plain)

    # 3. parse round-trip (the send_as_user Telethon edit path): collapsed survives
    patch_expandable_blockquotes()
    from telethon.extensions import html as tl_html
    text2, ents2 = tl_html.parse(ht)
    bq2 = [e for e in ents2 if type(e).__name__ == "MessageEntityBlockquote"]
    check("parse restores collapsed=True", len(bq2) == 1 and bq2[0].collapsed is True,
          repr(bq2))
    check("parse round-trips offsets", bq2 and (bq2[0].offset, bq2[0].length) == (src_ents[0].offset, src_ents[0].length),
          repr(bq2))
    check("parse keeps plain quote non-collapsed",
          not [e for e in tl_html.parse(ht_plain)[1] if getattr(e, "collapsed", False)])

    # 4. placement: the expandable quote is still recognized as a blockquote —
    #    odds tag lands on the ML bet line, never inside the quote
    picks, odds = FIXTURE["picks"], FIXTURE["odds_by_pick"]
    tagged = _insert_odds(ht, picks, odds)
    lines = tagged.split("\n")
    bq = _blockquote_lines(lines)
    tag_lines = [i for i, l in enumerate(lines) if "[+160" in l or "[160" in l]
    check("odds tag placed", bool(tag_lines), tagged[:200])
    check("odds tag outside the expandable quote", all(i not in bq for i in tag_lines),
          f"tag on lines {tag_lines}, bq lines {sorted(bq)}")
    check("tag on the ML line", all("ML" in lines[i] for i in tag_lines),
          "\n".join(lines[i] for i in tag_lines))

    # 5. idempotency: re-running the rebuild over the already-tagged HTML
    #    changes nothing (the tracker re-derives the edit every cycle)
    check("_insert_odds idempotent", _insert_odds(tagged, picks, odds) == tagged)

    # 6. emoji placement respects the expandable quote too
    emojied = _insert_emojis(tagged, [(p, "WIN", "", None) for p in picks])
    em_lines = [i for i, l in enumerate(emojied.split("\n")) if "✅" in l]
    check("emojis outside the expandable quote",
          em_lines and all(i not in _blockquote_lines(emojied.split("\n")) for i in em_lines),
          f"emoji on lines {em_lines}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} — {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
