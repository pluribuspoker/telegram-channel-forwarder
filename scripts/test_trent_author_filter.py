"""Regression test: the Trent watcher only forwards tweets Trent actually wrote.

Self-contained (no X/Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_trent_author_filter.py

`api.user_tweets(uid)` does NOT yield only that user's tweets. twscrape builds
its result by flattening EVERY Tweet object in the GraphQL response (`to_old_rep`
walks it recursively), so a tweet Trent *quotes* comes back as its own top-level
item authored by someone else. Retweets are dropped by id (`retweeted_ids`);
quotes are not, and `_is_retweet` can't catch them either — quoted text carries
no "RT @" prefix.

2026-08-08 is the case this pins: Trent quote-tweeted @krabs_bookit's "Outlaws
-1.5 is a whale" to fade it, and the watcher forwarded BOTH — Krabs' pick as
Trent's own, under a URL built from Trent's handle, while Trent had bet the
opposite side of the same game (Atlas +1.5). Nothing downstream can flag that:
the fabricated URL 307s to the real author so it resolves, and the pick prices
and grades exactly like a real one — into the wrong capper's record.

Also pins that a foreign tweet never reaches the `old_streak` counter. Quoted
tweets are usually older than the lookback window, so counting them can trip the
"3 consecutive old tweets" break and silently truncate the scan, dropping real
picks that sit behind them.
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import trent_watcher as tw_mod

NOW = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=2)


def _tweet(tid, author, text, when, photos=()):
    media = SimpleNamespace(photos=[SimpleNamespace(url=u) for u in photos]) if photos else None
    return SimpleNamespace(
        id=tid, date=when, rawContent=text,
        user=SimpleNamespace(username=author), media=media,
    )


def _run(stream):
    """Drive the real _fetch_impl over a canned timeline."""
    class _FakeAPI:
        async def user_by_login(self, name):
            return SimpleNamespace(id=1, username="BookitWithTrent")

        async def user_tweets(self, uid, limit=-1):
            for t in stream:
                yield t

    async def _fake_build_api():
        return _FakeAPI()

    orig = tw_mod.build_api
    tw_mod.build_api = _fake_build_api
    try:
        return asyncio.run(tw_mod._fetch_impl(SINCE, 20))
    finally:
        tw_mod.build_api = orig


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  ' + detail}")
    return cond


def main():
    print(f"module under test: {tw_mod.__file__}\n")
    ok = True

    # ── The real 2026-08-08 timeline: Trent's quote tweet, plus the quoted
    #    tweet twscrape flattens in beside it.
    print("quote-tweet fade (the 2026-08-08 case)")
    got = _run([
        _tweet(2086116921108205609, "BookitWithTrent",
               "I start fading Krabs...\n\nAtlas +1.5 is a mortal mega.",
               NOW - timedelta(minutes=14)),
        _tweet(2086113960034500618, "krabs_bookit",
               "Massive game on ABC today \n\nOutlaws -1.5 is a whale",
               NOW - timedelta(minutes=26), photos=["https://pbs.twimg.com/x.jpg"]),
    ])
    ok &= check("only Trent's tweet is returned", len(got) == 1, f"got {len(got)}")
    ok &= check("it is the Atlas pick, not Krabs' Outlaws",
                got and "Atlas +1.5" in got[0]["text"],
                got[0]["text"][:60] if got else "<empty>")
    ok &= check("Krabs' tweet id is absent",
                all(g["id"] != "2086113960034500618" for g in got))

    # ── A URL is built from the tweet's own author. x.com 307s a wrong handle
    #    to the right one, so a fabricated URL resolves and looks correct.
    print("\nurl authorship")
    got = _run([_tweet(999, "BookitWithTrent", "Braves ML mortal mega", NOW - timedelta(minutes=5))])
    ok &= check("own tweet -> own handle",
                got and got[0]["url"] == "https://x.com/BookitWithTrent/status/999",
                got[0]["url"] if got else "<empty>")

    # ── A quoted tweet is usually older than the window. It must not consume
    #    the old_streak budget, or it truncates the scan over real picks.
    print("\nold_streak isolation")
    old = NOW - timedelta(hours=9)
    got = _run([
        _tweet(1, "krabs_bookit", "old quoted A", old),
        _tweet(2, "someone_else", "old quoted B", old),
        _tweet(3, "another_acct", "old quoted C", old),
        _tweet(4, "BookitWithTrent", "REAL PICK behind three old quotes", NOW - timedelta(minutes=30)),
    ])
    ok &= check("three old foreign tweets don't break the scan",
                len(got) == 1 and got[0]["id"] == "4", f"got {[g['id'] for g in got]}")

    # ── Trent's OWN old tweets must still stop the scan (pinned-tweet guard).
    got = _run([
        _tweet(1, "BookitWithTrent", "old own 1", old),
        _tweet(2, "BookitWithTrent", "old own 2", old),
        _tweet(3, "BookitWithTrent", "old own 3", old),
        _tweet(4, "BookitWithTrent", "never reached", NOW - timedelta(minutes=30)),
    ])
    ok &= check("three of Trent's own old tweets still break the scan",
                got == [], f"got {[g['id'] for g in got]}")

    # ── Handle comparison is case-insensitive (X preserves display casing).
    print("\ncasing")
    got = _run([_tweet(7, "bookitwithtrent", "lowercase handle", NOW - timedelta(minutes=5))])
    ok &= check("lowercase handle still matches", len(got) == 1, f"got {len(got)}")

    print("\n" + ("ALL PASS" if ok else "FAILURES"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
