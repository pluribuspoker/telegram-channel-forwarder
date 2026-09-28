"""Regression test: a weekday in capper record flair is not a day hint.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_day_hint.py

YDC Free Play posted "Buccaneers +2" on Sunday 2026-09-27 with his record
block attached — "…\n22-13 Saturday\n6-0 NFL", a blockquote. _day_hint_date
took that "Saturday" as the bet's day, resolved it FORWARD to 2026-10-03
(next Saturday), and because the hint outranks the odds game_date the bind
window moved to Oct 2–6: the leg bound to NEXT week's Packers game instead
of that afternoon's Vikings game, never graded, and would have graded (and
broadcast) the wrong game on Oct 4.

Two nets, both pinned here:
  * blockquotes are angle records, never the bet — a weekday inside one is
    ignored (entity spans stripped before the scan);
  * outside a blockquote, a weekday directly after a W-L(-T) record token
    ("22-13 Saturday", "10-5-1 on Sunday") is flair, skipped in favor of the
    next weekday mention (if any).
Legit hints must keep working: "SATURDAY MAX PLAY" posted Friday, "LSU ML
-150 (Saturday)" (an odds token is not a record).
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon.tl.types import MessageEntityBlockquote

from tracker import _day_hint_date

# Live bytes + entity from -1002486251914:3903 (fan-out sibling -1004427337587:386).
YDC_TEXT = ("YDC Free Play\n\nBuccaneers +2 [-126]\n\nWWL-WWL-WWL-W last 10 picks \n"
            "40-22 off 1 win\n22-13 Saturday\n6-0 NFL")
YDC_ENTS = [MessageEntityBlockquote(offset=37, length=67)]

CFL_PREFIX = "Andrew Cunningham\n\nCFL SATURDAY MAX PLAY‼️ \n\n"
CFL_TEXT = CFL_PREFIX + "YTD CFL: 25-9"
CFL_ENTS = [MessageEntityBlockquote(offset=len(CFL_PREFIX), length=len("YTD CFL: 25-9"))]

# An astral emoji before the blockquote: entity offsets count UTF-16 units, so
# a str-index cut would slice the wrong span and let "Saturday" through.
EMOJI_PREFIX = "🔥 SUNDAY PLAY 🔥\nBucs +2\n\n"
EMOJI_TEXT = EMOJI_PREFIX + "22-13 Saturday"
EMOJI_ENTS = [MessageEntityBlockquote(offset=len(EMOJI_PREFIX.encode("utf-16-le")) // 2,
                                      length=len("22-13 Saturday"))]

CASES = [
    # (name, text, entities, msg_date, expected)
    ("the incident: 'Saturday' in the blockquoted record block is ignored",
     YDC_TEXT, YDC_ENTS, date(2026, 9, 27), None),
    ("same record flair with no blockquote entity still isn't a hint",
     YDC_TEXT, None, date(2026, 9, 27), None),
    ("record with ties skipped too", "Chiefs ML\n10-5-1 Saturday", None,
     date(2026, 9, 25), None),
    ("'on <day>' record phrasing skipped", "Bucs +2\n22-13 on Saturday", None,
     date(2026, 9, 27), None),
    ("flair weekday skipped, later real hint still taken",
     "22-13 on Saturday\nSUNDAY LOCK: Bucs +2", None, date(2026, 9, 25), "2026-09-27"),
    ("headline hint keeps working: SATURDAY play posted Friday",
     CFL_TEXT, CFL_ENTS, date(2026, 9, 18), "2026-09-19"),
    ("an odds token before the weekday is not a record: LSU ML -150 (Saturday)",
     "TWG\n\nLSU ML -150 (Saturday)\n\n8-1 this month", None, date(2026, 9, 18),
     "2026-09-19"),
    ("same-day mention still means no override",
     "SUNDAY BEST BET: Bucs +2", None, date(2026, 9, 27), None),
    ("blockquote offsets honor UTF-16 (astral emoji before the quote)",
     EMOJI_TEXT, EMOJI_ENTS, date(2026, 9, 26), "2026-09-27"),
    ("weekday in a quote that is not a record is still ignored",
     "Bucs +2\nSaturday night lights", [MessageEntityBlockquote(offset=8, length=21)],
     date(2026, 9, 27), None),
    ("no weekday at all", "Bucs +2 [-126]\n40-22 off 1 win", None,
     date(2026, 9, 27), None),
]


def main() -> int:
    failed = 0
    for name, text, ents, msg_date, want in CASES:
        got = _day_hint_date(text, msg_date, ents)
        ok = got == want
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
