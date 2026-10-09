"""Regression test: the Trent watcher never forwards a restatement of a pick it
already forwarded.

Self-contained (no X/Telegram/Claude) — run it directly:

    ~/venv/bin/python scripts/test_trent_repeat_veto.py

2026-10-08: "Me and @stevewilldoit have a combined $702,000 pending on Cowboys
spread sitting in a field suite." went out 4h after the "TNF MORTAL MEGA MAX:
COWBOYS -8.5" pick. The pick prompt's "I have $X on [team]" signal fired, and
nothing knew the bet was old — so a second, line-less Cowboys pick hit the
channel (odds watch: needs_human; nightly audit: parked ungraded). Same class
2026-10-02: "VT -2.5 is a MORTAL mega max @shadybiev" after the Virginia Tech
post.

The fix is a separate veto (`is_repeat`) that runs last, on forward candidates
only, against the texts of picks forwarded in the last _RECENT_PICKS_HOURS
(stored in trent_seen.text). Pins:
  - main() asks the veto with the earlier pick's text and drops the repeat
    (marked seen, had_pick=0) while the original still sends;
  - the veto is never asked without recent picks, and never about a non-pick;
  - an old trent_seen table (no `text` column) migrates in place;
  - only picks keep their text, t.co links stripped, window + age respected.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import trent_watcher as tw_mod

PICK = {
    "id": "2108278699955208256", "date": "2026-10-08T19:29:46+00:00", "photos": "",
    "url": "https://x.com/BookitWithTrent/status/2108278699955208256",
    "text": "TNF MORTAL MEGA MAX: \n\nCOWBOYS -8.5 (5U) ☢️💣🐳\n\nDON’T OVERTHINK IT. "
            "https://t.co/abc123",
}
FLEX = {
    "id": "2108341047239844290", "date": "2026-10-08T23:37:31+00:00", "photos": "",
    "url": "https://x.com/BookitWithTrent/status/2108341047239844290",
    "text": "Me and @stevewilldoit have a combined $702,000 pending on Cowboys spread "
            "sitting in a field suite.\n\nThis should go well. 💀",
}
CHATTER = {
    "id": "2108341047239844999", "date": "2026-10-08T23:40:00+00:00", "photos": "",
    "url": "https://x.com/BookitWithTrent/status/2108341047239844999",
    "text": "Field suite views are unreal tonight",
}


def _run_main(tweets, pick_ids, db_path):
    """Drive the real main() over canned tweets; returns (sent ids, repeat prompts)."""
    sent, prompts = [], []

    async def fake_fetch(since, limit=50):
        return list(tweets), False

    async def fake_is_pick_text(tweet):
        return tweet["id"] in pick_ids

    async def fake_send_pick(tweet, dest, dry_run=False):
        sent.append(tweet["id"])

    async def fake_trigger(channel):
        pass

    async def fake_claude(**kw):
        prompt = kw["messages"][0]["content"]
        prompts.append(prompt)
        old_pick = "COWBOYS -8.5" in prompt.split("New tweet:")[0]
        flex = "pending on Cowboys spread" in prompt.split("New tweet:")[1]
        answer = "repeat: COWBOYS -8.5 (5U)" if old_pick and flex else "new"
        return SimpleNamespace(content=[SimpleNamespace(text=answer)])

    saved = {k: getattr(tw_mod, k) for k in (
        "fetch_recent_tweets", "is_pick_text", "send_pick", "_trigger_tracker_soon",
        "_claude_create_with_retry", "DB_PATH")}
    saved_argv = sys.argv
    try:
        tw_mod.fetch_recent_tweets = fake_fetch
        tw_mod.is_pick_text = fake_is_pick_text
        tw_mod.send_pick = fake_send_pick
        tw_mod._trigger_tracker_soon = fake_trigger
        tw_mod._claude_create_with_retry = fake_claude
        tw_mod.DB_PATH = db_path
        sys.argv = ["trent_watcher.py"]
        asyncio.run(tw_mod.main())
    finally:
        for k, v in saved.items():
            setattr(tw_mod, k, v)
        sys.argv = saved_argv
    return sent, prompts


def _seen(db_path):
    con = sqlite3.connect(db_path)
    rows = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT tweet_id, had_pick, text FROM trent_seen")}
    con.close()
    return rows


def test_flex_after_pick_is_vetoed(tmp):
    db = os.path.join(tmp, "a.db")
    sent, prompts = _run_main([PICK, FLEX, CHATTER], {PICK["id"], FLEX["id"]}, db)
    assert sent == [PICK["id"]], sent
    # Asked once, about the flex only: the first pick had no recent picks to
    # repeat, and the chatter never was a pick candidate.
    assert len(prompts) == 1, len(prompts)
    assert "pending on Cowboys spread" in prompts[0]
    assert "https://t.co" not in prompts[0], "t.co links must be stripped from the list"
    seen = _seen(db)
    assert seen[PICK["id"]][0] == 1 and "COWBOYS -8.5" in seen[PICK["id"]][1]
    assert seen[FLEX["id"]] == (0, None), seen[FLEX["id"]]
    assert seen[CHATTER["id"]] == (0, None)


def test_repeat_across_runs(tmp):
    """The pick forwarded on an EARLIER run still vetoes the flex — the list
    comes from trent_seen, not from this run's memory."""
    db = os.path.join(tmp, "b.db")
    assert _run_main([PICK], {PICK["id"]}, db)[0] == [PICK["id"]]
    sent, prompts = _run_main([FLEX], {FLEX["id"]}, db)
    assert sent == [] and len(prompts) == 1


def test_new_pick_still_sends(tmp):
    db = os.path.join(tmp, "c.db")
    other = dict(FLEX, id="2108341047239800000", text="BUCS 1H +4.5 (2u)")
    sent, prompts = _run_main([PICK, other], {PICK["id"], other["id"]}, db)
    assert sent == [PICK["id"], other["id"]], sent
    assert len(prompts) == 1


def test_legacy_table_migrates(tmp):
    db = os.path.join(tmp, "d.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trent_seen (tweet_id TEXT PRIMARY KEY, "
                "processed_at TEXT NOT NULL, had_pick INTEGER NOT NULL DEFAULT 0)")
    con.execute("INSERT INTO trent_seen VALUES ('1', '2026-10-08T00:00:00+00:00', 1)")
    con.commit()
    con.close()
    saved = tw_mod.DB_PATH
    try:
        tw_mod.DB_PATH = db
        con = tw_mod._db()
        cols = {r[1] for r in con.execute("PRAGMA table_info(trent_seen)")}
        assert "text" in cols
        assert tw_mod._recent_picks(con) == []  # legacy rows have no text
        con.close()
    finally:
        tw_mod.DB_PATH = saved


def test_recent_window(tmp):
    con = sqlite3.connect(":memory:")
    con.execute(tw_mod._SCHEMA)
    now = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
    for tid, hours, had_pick, text in (
        ("old", tw_mod._RECENT_PICKS_HOURS + 1, 1, "BEARS -3"),
        ("a", 6, 1, "COWBOYS -8.5 https://t.co/x"),
        ("b", 2, 1, "WHITE SOX ML"),
        ("nopick", 1, 0, "chatter"),
    ):
        con.execute("INSERT INTO trent_seen VALUES (?, ?, ?, ?)",
                    (tid, (now - timedelta(hours=hours)).isoformat(), had_pick, text))
    got = tw_mod._recent_picks(con, now=now)
    assert [round(h) for h, _ in got] == [6, 2], got
    assert [t for _, t in got] == ["COWBOYS -8.5 https://t.co/x", "WHITE SOX ML"]


def test_no_recent_skips_call():
    async def boom(**kw):
        raise AssertionError("no recent picks → no Claude call")
    saved = tw_mod._claude_create_with_retry
    try:
        tw_mod._claude_create_with_retry = boom
        assert asyncio.run(tw_mod.is_repeat(FLEX, [])) is False
    finally:
        tw_mod._claude_create_with_retry = saved


def main():
    tests = [test_flex_after_pick_is_vetoed, test_repeat_across_runs, test_new_pick_still_sends,
             test_legacy_table_migrates, test_recent_window, test_no_recent_skips_call]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                t(tmp) if t.__code__.co_argcount else t()
                print(f"PASS {t.__name__}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
