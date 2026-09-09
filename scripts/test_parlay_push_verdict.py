"""Regression: a pushed parlay leg drops out of the ticket — WIN+PUSH is a WIN.

Incident (-1002486251914:3794 + -1004427337587:263, 2026-09-08): "Brewers F5 ML /
Yankees F5 ML parlay" — Brewers tied 1-1 after 5 (PUSH), Yankees led 2-1 (WIN).
Standard parlay rules void the pushed leg and reduce the ticket to the remaining
legs, so the parlay WINS at the Yankees leg's price. All three copies of the
aggregation rule ("all legs must WIN, else any PUSH → PUSH") graded it ♻️:
the live-message emoji (tracker_format via _overall_verdict), the broadcast
(audit._overall_emoji), and the Sheets row (inline in sheets.py). The fix
single-sources the rule in tracker_grading._overall_verdict and prices the
reduced ticket from the non-pushed legs only.

Message text and picks are byte-exact copies of the live message / cache entry.
Offline — no network, no API spend.

    ~/venv/bin/python scripts/test_parlay_push_verdict.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grade_daemon as gd  # noqa: E402
from audit import AuditLog  # noqa: E402
from tracker_format import _insert_emojis  # noqa: E402
from tracker_grading import _overall_verdict  # noqa: E402

passed = failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [ok] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


# ── the verdict rule itself ──────────────────────────────────────────────────
P = {"is_parlay_leg": True}
S = {}  # standalone


def ov(*verdicts, pick=P):
    return _overall_verdict([(pick, v) for v in verdicts])


check("parlay WIN+PUSH → WIN (the incident)", ov("WIN", "PUSH") == "WIN")
check("parlay PUSH+WIN → WIN (order-independent)", ov("PUSH", "WIN") == "WIN")
check("parlay all-push → PUSH", ov("PUSH", "PUSH") == "PUSH")
check("parlay all-win → WIN", ov("WIN", "WIN") == "WIN")
check("parlay PUSH+LOSS → LOSS", ov("PUSH", "LOSS") == "LOSS")
check("parlay PUSH+PENDING → PENDING", ov("PUSH", "PENDING") == "PENDING")
check("parlay WIN+PUSH+PENDING → PENDING", ov("WIN", "PUSH", "PENDING") == "PENDING")
check("parlay WIN+PUSH+UNKNOWN → UNKNOWN", ov("WIN", "PUSH", "UNKNOWN") == "UNKNOWN")
check("non-parlay WIN+PUSH still WIN", ov("WIN", "PUSH", pick=S) == "WIN")
check("non-parlay all-push still PUSH", ov("PUSH", "PUSH", pick=S) == "PUSH")
check("non-parlay WIN+LOSS still UNKNOWN", ov("WIN", "LOSS", pick=S) == "UNKNOWN")

# ── incident fixtures (byte-exact from parse_cache -1002486251914:3794) ──────
BREWERS = {
    "description": "Milwaukee Brewers F5 moneyline (parlay leg)", "sport": "MLB",
    "bet_type": "moneyline", "is_parlay_leg": True, "period": "1h",
    "teams": ["Milwaukee Brewers"], "player": None, "prop_stat": None,
    "line": None, "direction": None,
}
YANKEES = {
    "description": "New York Yankees F5 moneyline (parlay leg)", "sport": "MLB",
    "bet_type": "moneyline", "is_parlay_leg": True, "period": "1h",
    "teams": ["New York Yankees"], "player": None, "prop_stat": None,
    "line": None, "direction": None,
}
VERDICTS = [(BREWERS, "PUSH", "[1H complete] Milwaukee Brewers 1 vs 1 1H — exact, push", "MLB", "2026-09-08"),
            (YANKEES, "WIN", "[1H complete] New York Yankees 2 vs 1 1H -> +1", "MLB", "2026-09-08")]

CLEAN = "Empire\n\nBrewers F5 ML / Yankees F5 ML parlay (-125) 0.5u"

# ── live-message emoji: the parlay line gets ONE ✅, idempotently ────────────
out = _insert_emojis(CLEAN, VERDICTS)
check("live message stamps ✅ on the parlay line", out == CLEAN + "✅", repr(out))
again = _insert_emojis(out, VERDICTS)
check("re-edit is a no-op", again == out, repr(again))

all_push = [(p, "PUSH", c, s, d) for p, _v, c, s, d in VERDICTS]
check("all-push live message stamps ♻️",
      _insert_emojis(CLEAN, all_push) == CLEAN + "♻️",
      repr(_insert_emojis(CLEAN, all_push)))

# ── broadcast: one ticket, ✅ overall, pushed leg marked, reduced price ──────


def run_broadcast(leg_verdicts_by_idx: dict) -> list[dict]:
    audit = AuditLog(
        db_path=str(Path(tempfile.mkdtemp()) / "test_audit.db"),
        bot_token="TESTTOKEN",
        broadcast_results_mappings={-1002486251914: -2001},
    )
    posts: list[dict] = []

    async def fake_post(*, target, text, reply_to_id, link):
        posts.append({"target": target, "text": text})

    audit._post_broadcast = fake_post
    gd._save_pending_cache = lambda cache: None  # never touch the real parse_cache.json

    leg_verdicts = {
        str(i): {"verdict": v, "sport": "MLB", "game_date": "2026-09-08"}
        for i, v in leg_verdicts_by_idx.items()
    }
    odds_by_pick = {"0": {"odds": -218}, "1": {"odds": -310}}
    picks = [BREWERS, YANKEES]
    pending: list[dict] = []
    gd._queue_broadcast(
        pending, cache_key="-1002486251914:3794", channel_id=-1002486251914,
        message_id=3794, capper="Empire", reply_to_id=None,
        bc_results=gd._parlay_broadcast_legs(picks, leg_verdicts, odds_by_pick, "MLB"),
        sheets_results=[], leg_indices=[0, 1], mark_all_resolved=True,
        html_text="", msg_date="2026-09-08", sport="MLB",
        picks=picks, leg_verdicts=leg_verdicts, odds_by_pick=odds_by_pick,
    )

    class NoESPN:
        async def get(self, sport, game_date):
            return None

    cache = {"-1002486251914:3794": {"leg_verdicts": leg_verdicts}}
    asyncio.run(gd._flush_broadcasts(
        pending, audit, cache, sheets_map={}, espn_cache=NoESPN(),
    ))
    return posts


posts = run_broadcast({0: "PUSH", 1: "WIN"})
EXPECTED = ('✅ <b><a href="https://t.me/c/2486251914/3794">Empire</a></b>'
            ' · Parlay: Milwaukee Brewers F5 ML ♻️ / New York Yankees F5 ML [-310]')
check("broadcast is ✅ with pushed leg marked and the reduced price",
      len(posts) == 1 and posts[0]["text"] == EXPECTED,
      repr(posts))

posts = run_broadcast({0: "PUSH", 1: "PUSH"})
EXPECTED_ALL_PUSH = ('♻️ <b><a href="https://t.me/c/2486251914/3794">Empire</a></b>'
                     ' · Parlay: Milwaukee Brewers F5 ML ♻️ / New York Yankees F5 ML ♻️')
check("all-push broadcast is ♻️ with no price (whole stake refunded)",
      len(posts) == 1 and posts[0]["text"] == EXPECTED_ALL_PUSH,
      repr(posts))

posts = run_broadcast({0: "PUSH", 1: "LOSS"})
EXPECTED_LOSS = ('❌ <b><a href="https://t.me/c/2486251914/3794">Empire</a></b>'
                 ' · Parlay: Milwaukee Brewers F5 ML ♻️ / New York Yankees F5 ML [-310]')
check("push+loss broadcast is ❌ priced on the live leg",
      len(posts) == 1 and posts[0]["text"] == EXPECTED_LOSS,
      repr(posts))

posts = run_broadcast({0: "WIN", 1: "WIN"})
EXPECTED_SWEEP = ('✅ <b><a href="https://t.me/c/2486251914/3794">Empire</a></b>'
                  ' · Parlay: Milwaukee Brewers F5 ML / New York Yankees F5 ML [-108]')
check("all-win broadcast unchanged (full combined price, no markers)",
      len(posts) == 1 and posts[0]["text"] == EXPECTED_SWEEP,
      repr(posts))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
