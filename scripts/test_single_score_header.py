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
  4. two results on the same game, final  -> merged into one message with score header
  5. two results, game still in progress  -> merged into ONE message, but headerless —
     the title header is exclusively the final-score format

Multi-pick messages (2026-09-08, operator request — Cunningham's FSU +4.5/FSU ML
posted apart from the merged SMU–FSU broadcast): a multi-pick whose legs ALL land
on one game joins the game group, one line per leg; legs spanning games (or a
parlay — one ticket, one price) keep the compact per-message path:

  6. multi-pick, both legs one game, final     -> group format, one line per leg
  7. multi-pick + a single on the same game    -> ONE merged message, 3 lines
  8. multi-pick legs on different games        -> compact multi-pick message
  9. parlay on one game                        -> compact "Parlay:" line, unchanged
 10. multi-pick one game, still in progress    -> compact (header only over finals)
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


def queue_multi(pending, *, message_id: int, capper: str, second_teams: list[str],
                parlay: bool = False) -> None:
    """A two-leg source message: Cubs ML (WIN) + an Under on `second_teams` (LOSS).

    The ML leg names only the bet side while the total names its matchup, so the
    legs' name keys differ — same-game membership must come from the ESPN event
    upgrade, exactly like the production case it pins.
    """
    p1 = {"bet_type": "moneyline", "teams": ["Chicago Cubs"], "sport": "MLB",
          "period": "game", "description": "Chicago Cubs ML"}
    p2 = {"bet_type": "total", "teams": second_teams, "sport": "MLB",
          "period": "game", "line": 8.5, "direction": "under",
          "description": " / ".join(second_teams) + " Under 8.5"}
    if parlay:
        p1["is_parlay_leg"] = p2["is_parlay_leg"] = True
    gd._queue_broadcast(
        pending, cache_key="k1", channel_id=-1001, message_id=message_id,
        capper=capper, reply_to_id=None,
        bc_results=[(p1, "WIN", -150), (p2, "LOSS", -110)], sheets_results=[],
        leg_indices=[0, 1], mark_all_resolved=False, html_text="",
        msg_date=TODAY, sport="MLB", picks=[p1, p2],
        leg_verdicts={"0": {"verdict": "WIN", "sport": "MLB", "game_date": TODAY},
                      "1": {"verdict": "LOSS", "sport": "MLB", "game_date": TODAY}},
        odds_by_pick={"0": {"odds": -150}, "1": {"odds": -110}},
    )


def run_flush_queued(scoreboard, queue_fn) -> list[dict]:
    """Run queue_fn to fill the pending list, flush, return captured posts."""
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
    queue_fn(pending)
    cache = {"k1": {"leg_verdicts": {"0": {"verdict": "WIN"}}}}

    asyncio.run(gd._flush_broadcasts(
        pending, audit, cache, sheets_map={}, espn_cache=FakeESPNCache(scoreboard),
    ))
    return posts


def run_flush(scoreboard, n_items: int) -> list[dict]:
    """Queue n_items lone results on the same game, flush, return captured posts."""
    cappers = ["Midwest Mike", "Tony"]

    def q(pending):
        for i in range(n_items):
            queue_one(pending, message_id=111 + i, capper=cappers[i])

    return run_flush_queued(scoreboard, q)


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

# ── 5. two cappers, game in progress -> ONE merged message, NO header ─────────
# The title header is exclusively the final-score format (2026-09-07, "WIS VS
# ND" over two mid-game-settled unders read as a final missing its score). A
# scoreless merge still posts once — merging is the notification knob — but as
# bare pick lines: no <b><u> title, no matchup, no running score.
posts = run_flush({"events": [make_event(completed=False)]}, n_items=2)
text = posts[0]["text"] if posts else ""
print("merged+live:", text.replace("\n", " | "))
check("merge+live posts once", len(posts) == 1, f"{len(posts)} posts")
check("merge+live has no header", "<u>" not in text and "Marlins" not in text
      and not text.startswith("\n"), text)
check("merge+live keeps both pick lines",
      text.count("✅ Chicago Cubs ML [-167]") >= 1
      and "Midwest Mike" in text and "Tony" in text, text)

# ── 6. multi-pick, both legs on ONE game, final -> group format, line per leg ─
# The legs' name keys differ (bet side vs matchup) — only the ESPN event upgrade
# can merge them, so this also pins the per-leg resolution path.
posts = run_flush_queued(
    {"events": [make_event(completed=True)]},
    lambda pending: queue_multi(pending, message_id=311, capper="Andrew C",
                                second_teams=["Miami Marlins", "Chicago Cubs"]))
text = posts[0]["text"] if posts else ""
print("multi+final:", text.replace("\n", " | "))
check("multi+final posts once", len(posts) == 1, f"{len(posts)} posts")
check("multi+final has score header", "<b><u>" in text and "Marlins 1–6 Cubs" in text, text)
check("multi+final one line per leg", text.count("— <a href=") == 2, text)
check("multi+final keeps both verdicts", "✅" in text and "❌" in text, text)

# ── 7. multi-pick + a lone single on the same game -> ONE merged message ──────
def _q7(pending):
    queue_one(pending, message_id=111, capper="Midwest Mike")
    queue_multi(pending, message_id=311, capper="Andrew C",
                second_teams=["Miami Marlins", "Chicago Cubs"])
posts = run_flush_queued({"events": [make_event(completed=True)]}, _q7)
text = posts[0]["text"] if posts else ""
print("multi+single:", text.replace("\n", " | "))
check("multi+single posts once", len(posts) == 1, f"{len(posts)} posts")
check("multi+single has score header", "Marlins 1–6 Cubs" in text, text)
check("multi+single has 3 pick lines", text.count("— <a href=") == 3, text)
check("multi+single names both cappers", "Midwest Mike" in text and "Andrew C" in text, text)

# ── 8. multi-pick legs on DIFFERENT games -> compact multi-pick message ───────
# Second leg's team is on no scoreboard event: its name key survives the upgrade,
# the keys differ, and the message must stay whole on the per-message path.
posts = run_flush_queued(
    {"events": [make_event(completed=True)]},
    lambda pending: queue_multi(pending, message_id=311, capper="Andrew C",
                                second_teams=["Boston Red Sox"]))
text = posts[0]["text"] if posts else ""
print("multi+split:", text.replace("\n", " | "))
check("split-game multi posts once", len(posts) == 1, f"{len(posts)} posts")
check("split-game multi stays compact",
      "<u>" not in text and text.startswith("<b><a href="), text)
check("split-game multi keeps both legs", "✅" in text and "❌" in text, text)

# ── 9. parlay on one game -> compact Parlay line, unchanged ───────────────────
posts = run_flush_queued(
    {"events": [make_event(completed=True)]},
    lambda pending: queue_multi(pending, message_id=311, capper="Andrew C",
                                second_teams=["Miami Marlins", "Chicago Cubs"],
                                parlay=True))
text = posts[0]["text"] if posts else ""
print("parlay:", text.replace("\n", " | "))
check("parlay posts once", len(posts) == 1, f"{len(posts)} posts")
check("parlay stays compact Parlay line", "Parlay:" in text and "<u>" not in text, text)

# ── 10. multi-pick one game, still in progress -> compact, never a header ─────
posts = run_flush_queued(
    {"events": [make_event(completed=False)]},
    lambda pending: queue_multi(pending, message_id=311, capper="Andrew C",
                                second_teams=["Miami Marlins", "Chicago Cubs"]))
text = posts[0]["text"] if posts else ""
print("multi+live:", text.replace("\n", " | "))
check("multi+live posts once", len(posts) == 1, f"{len(posts)} posts")
check("multi+live stays compact",
      "<u>" not in text and text.startswith("<b><a href="), text)

print()
if failures:
    print(f"❌ {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ all single-score-header cases pass")
