"""Regression test: lone results with a known final render via the score-header format.

Self-contained (no Telegram/API — the Bot API poster is stubbed out) — run directly:

    ~/venv/bin/python scripts/test_single_score_header.py

The merged multi-capper broadcast leads with the game's final score
("⚾️ Marlins 1–6 Cubs") and puts the capper after the dash; single results used
to render only the compact one-liner, so whether a reader saw the score depended
on how many cappers happened to hit the same game in the same cycle. Now the
rule is coherent and this pins it:

  1. lone result + completed ESPN event  -> group format (score header, capper after dash)
  2. lone result + game still in progress -> compact line (never print a running score)
  3. lone result + no ESPN event at all   -> compact line (CFL/KBO, offseason, no match)
  4. two results on the same game         -> merged into one message, unchanged
"""
import asyncio
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grade_daemon as gd
from audit import AuditLog

TODAY = date.today().isoformat()


def make_event(completed: bool) -> dict:
    return {
        "id": "401",
        "competitions": [{
            "status": {"type": {"completed": completed}},
            "competitors": [
                {"homeAway": "away", "score": "1",
                 "team": {"displayName": "Miami Marlins", "shortDisplayName": "Marlins"}},
                {"homeAway": "home", "score": "6",
                 "team": {"displayName": "Chicago Cubs", "shortDisplayName": "Cubs"}},
            ],
        }],
    }


class FakeESPNCache:
    def __init__(self, scoreboard):
        self.scoreboard = scoreboard

    async def get(self, sport, game_date):
        return self.scoreboard


def queue_one(pending, *, message_id: int, capper: str) -> None:
    pick = {"bet_type": "moneyline", "teams": ["Chicago Cubs"], "sport": "MLB",
            "period": "game", "description": "Chicago Cubs ML"}
    gd._queue_broadcast(
        pending, cache_key="k1", channel_id=-1001, message_id=message_id,
        capper=capper, reply_to_id=None,
        bc_results=[(pick, "WIN", -167)], sheets_results=[],
        leg_indices=[0], mark_all_resolved=False, html_text="",
        msg_date=TODAY, sport="MLB", picks=[pick],
        leg_verdicts={"0": {"verdict": "WIN", "sport": "MLB", "game_date": TODAY}},
        odds_by_pick={"0": {"odds": -167}},
    )


def run_flush(scoreboard, n_items: int) -> list[dict]:
    """Queue n_items results on the same game, flush, return captured posts."""
    audit = AuditLog(
        db_path=str(Path(tempfile.mkdtemp()) / "test_audit.db"),
        bot_token="TESTTOKEN",
        broadcast_results_mappings={-1001: -2001},
    )
    posts: list[dict] = []

    async def fake_post(*, target, text, reply_to_id, link):
        posts.append({"target": target, "text": text, "reply_to_id": reply_to_id})

    audit._post_broadcast = fake_post
    gd._save_pending_cache = lambda cache: None  # never touch the real parse_cache.json

    pending: list[dict] = []
    cappers = ["Midwest Mike", "Tony"]
    for i in range(n_items):
        queue_one(pending, message_id=111 + i, capper=cappers[i])
    cache = {"k1": {"leg_verdicts": {"0": {"verdict": "WIN"}}}}

    asyncio.run(gd._flush_broadcasts(
        pending, audit, cache, sheets_map={}, espn_cache=FakeESPNCache(scoreboard),
    ))
    return posts


failures = []


def check(name: str, cond: bool, detail: str) -> None:
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}")
    if not cond:
        failures.append(f"{name}: {detail}")


# ── 1. lone result, game final -> score header via the group renderer ─────────
posts = run_flush({"events": [make_event(completed=True)]}, n_items=1)
text = posts[0]["text"] if posts else ""
print("single+final:", text.replace("\n", " | "))
check("single+final posts once", len(posts) == 1, f"{len(posts)} posts")
check("single+final has score header", "<b><u>" in text and "Marlins 1–6 Cubs" in text, text)
check("single+final capper after dash", "— <a href=" in text and "Midwest Mike" in text, text)
check("single+final pick line", "✅ Chicago Cubs ML [-167]" in text, text)

# ── 2. lone result, game in progress -> compact line, no running score ────────
posts = run_flush({"events": [make_event(completed=False)]}, n_items=1)
text = posts[0]["text"] if posts else ""
print("single+live:", text.replace("\n", " | "))
check("single+live posts once", len(posts) == 1, f"{len(posts)} posts")
check("single+live stays compact", "<u>" not in text and " · " in text, text)
check("single+live leads with capper", text.startswith("✅ <b><a href="), text)

# ── 3. lone result, no ESPN event -> compact line ─────────────────────────────
posts = run_flush({"events": []}, n_items=1)
text = posts[0]["text"] if posts else ""
check("single+no-event posts once", len(posts) == 1, f"{len(posts)} posts")
check("single+no-event stays compact", "<u>" not in text and " · " in text, text)

# ── 4. two cappers on the game -> still ONE merged message ────────────────────
posts = run_flush({"events": [make_event(completed=True)]}, n_items=2)
text = posts[0]["text"] if posts else ""
print("merged:", text.replace("\n", " | "))
check("merge posts once", len(posts) == 1, f"{len(posts)} posts")
check("merge has score header", "Marlins 1–6 Cubs" in text, text)
check("merge names both cappers", "Midwest Mike" in text and "Tony" in text, text)

print()
if failures:
    print(f"❌ {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ all single-score-header cases pass")
