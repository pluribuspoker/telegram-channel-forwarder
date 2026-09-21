"""Regression test: cross-post dedupe of broadcast result lines.

Self-contained (no Telegram/API — the Bot API poster is stubbed) — run directly:

    ~/venv/bin/python scripts/test_broadcast_dedupe.py

Incident 2026-09-20 (Midwest Mike, -1002486251914): the capper posted the
Colts/Chiefs U47.5 as its own message (:3864), then an evening recap (:3868)
restating the same pick beside the parlay and the Broncos ML. Both messages
parsed, graded, and broadcast independently — the ❌ U47.5 result posted TWICE
at 10:56 PM. Every existing guard is per-message (`broadcasted` flags,
`_post_fingerprint` keyed on channel+message), so a restated bet from a NEW
message sails through all of them.

Pinned here: a result line's identity is (capper, verdict, formatted bet) per
target channel — price deliberately excluded (odds snapshots drift between the
copies' fetches). A line the target already carries within
BROADCAST_DEDUPE_DAYS is dropped in both senders (`broadcast_results` and
`broadcast_group` share the `broadcast_lines` table in picks.db), and a post
with nothing left is skipped whole.
"""
import asyncio
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit import AuditLog, BROADCAST_DEDUPE_DAYS, _format_pick

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  -> {detail}"))
    if not cond:
        failures.append(name)


# ── The incident's real picks (parse_cache -1002486251914:3864 / :3868) ──────

U475 = {
    "description": "Colts vs Chiefs Under 47.5 (-140)", "sport": "NFL",
    "bet_type": "total", "is_parlay_leg": False, "period": "game",
    "teams": ["Indianapolis Colts", "Kansas City Chiefs"],
    "player": None, "prop_stat": None, "line": 47.5, "direction": "under",
}
CHARGERS = {
    "description": "Chargers ML parlay leg", "sport": "NFL",
    "bet_type": "moneyline", "is_parlay_leg": True, "period": "game",
    "teams": ["Los Angeles Chargers"],
    "player": None, "prop_stat": None, "line": None, "direction": None,
}
RAMS = {
    "description": "Rams ML parlay leg", "sport": "NFL",
    "bet_type": "moneyline", "is_parlay_leg": True, "period": "game",
    "teams": ["Los Angeles Rams"],
    "player": None, "prop_stat": None, "line": None, "direction": None,
}
BRONCOS = {
    "description": "Broncos ML -140", "sport": "NFL",
    "bet_type": "moneyline", "is_parlay_leg": False, "period": "game",
    "teams": ["Denver Broncos"],
    "player": None, "prop_stat": None, "line": None, "direction": None,
}

TARGET = -2001


def mk(db_path: str, posts: list[str]) -> AuditLog:
    audit = AuditLog(
        db_path=db_path, bot_token="TESTTOKEN",
        broadcast_results_mappings={-1001: TARGET},
    )

    async def fake_post(*, target, text, reply_to_id, link):
        posts.append(text)

    audit._post_broadcast = fake_post
    return audit


def fresh_db() -> str:
    return str(Path(tempfile.mkdtemp()) / "test_audit.db")


def br(audit, msg_id, pick_results, capper="Midwest Mike"):
    asyncio.run(audit.broadcast_results(
        channel_id=-1001, message_id=msg_id, pick_results=pick_results,
        capper_name=capper, reply_to_id=777,
    ))


def bg(audit, items, header=""):
    asyncio.run(audit.broadcast_group(
        target_channel=TARGET, header=header, items=items, reply_to_id=777,
    ))


def item(pick, verdict, odds, capper="Midwest Mike", msg_id=1):
    return {"channel_id": -1001, "message_id": msg_id, "capper": capper,
            "pick": pick, "verdict": verdict, "odds": odds}


# ── 1-2: the incident, byte-real payloads ─────────────────────────────────────

db = fresh_db()
posts: list[str] = []
audit = mk(db, posts)

br(audit, 3864, [(U475, "LOSS", -141)])
check("first message's result posts", len(posts) == 1, repr(posts))
t = posts[0] if posts else ""
check("first post renders the unchanged compact line",
      t.startswith("❌ ") and " · " in t and "U47.5 [-141]" in t, t)

br(audit, 3868, [(U475, "LOSS", -141)])
check("recap restating the same pick does NOT post again (the incident)",
      len(posts) == 1, repr(posts))

# ── 3: price drift between the two copies still dedupes ──────────────────────

posts2: list[str] = []
audit2 = mk(fresh_db(), posts2)
br(audit2, 1, [(U475, "LOSS", -141)])
br(audit2, 2, [(U475, "LOSS", -139)])
check("odds snapshot drift (-141 vs -139) still dedupes", len(posts2) == 1, repr(posts2))

# ── 4-6: what must NOT dedupe ─────────────────────────────────────────────────

posts3: list[str] = []
audit3 = mk(fresh_db(), posts3)
br(audit3, 1, [(U475, "LOSS", -141)])
br(audit3, 2, [(U475, "LOSS", -141)], capper="Other Guy")
check("a DIFFERENT capper on the same bet posts", len(posts3) == 2, repr(posts3))
br(audit3, 3, [(BRONCOS, "WIN", -148)])
check("the same capper's DIFFERENT bet posts", len(posts3) == 3, repr(posts3))

posts4: list[str] = []
audit4 = mk(fresh_db(), posts4)
br(audit4, 1, [(U475, "LOSS", -141)])
br(audit4, 2, [(U475, "WIN", -141)])
check("a different VERDICT posts (verdict is part of the identity)",
      len(posts4) == 2, repr(posts4))

# ── 7-8: parlay ticket restated ───────────────────────────────────────────────

TICKET = [(CHARGERS, "LOSS", -325), (RAMS, "PENDING", None)]
posts5: list[str] = []
audit5 = mk(fresh_db(), posts5)
br(audit5, 1, TICKET)
check("settled ticket posts once", len(posts5) == 1 and "Parlay:" in posts5[0],
      repr(posts5))
br(audit5, 2, TICKET)
check("restated ticket does NOT post again", len(posts5) == 1, repr(posts5))
br(audit5, 3, TICKET + [(BRONCOS, "WIN", -148)])
check("restated ticket + a NEW straight posts only the straight, compact",
      len(posts5) == 2 and "Parlay" not in posts5[1] and " · " in posts5[1],
      repr(posts5))

# ── 9-11: broadcast_group shares the same memory ─────────────────────────────

posts6: list[str] = []
audit6 = mk(fresh_db(), posts6)
br(audit6, 1, [(U475, "LOSS", -141)])
bg(audit6, [item(U475, "LOSS", -141, msg_id=2),
            item(U475, "LOSS", -141, capper="Other Guy", msg_id=3)])
check("group drops the line a compact post already carried, keeps the other capper",
      len(posts6) == 2 and "Other Guy" in posts6[1] and "Midwest" not in posts6[1],
      repr(posts6))
bg(audit6, [item(U475, "LOSS", -141, msg_id=4)])
check("group with nothing new skips the post entirely", len(posts6) == 2, repr(posts6))

posts7: list[str] = []
audit7 = mk(fresh_db(), posts7)
bg(audit7, [item(U475, "LOSS", -141, msg_id=1),
            item(U475, "LOSS", -141, msg_id=2)])
check("one capper restated INSIDE one group collapses to one link",
      len(posts7) == 1 and posts7[0].count("Midwest Mike") == 1, repr(posts7))

# ── 12: the window expires ────────────────────────────────────────────────────

posts8: list[str] = []
db8 = fresh_db()
audit8 = mk(db8, posts8)
br(audit8, 1, [(U475, "LOSS", -141)])
old = (datetime.now(timezone.utc)
       - timedelta(days=BROADCAST_DEDUPE_DAYS + 1)).isoformat()
with sqlite3.connect(db8) as conn:
    conn.execute("UPDATE broadcast_lines SET sent_at = ?", (old,))
    conn.commit()
br(audit8, 2, [(U475, "LOSS", -141)])
check("past the window the same line may post again", len(posts8) == 2, repr(posts8))

# ── 13: memory survives a process restart (new AuditLog, same DB) ────────────

posts9: list[str] = []
audit9 = mk(db, posts9)  # the incident DB from cases 1-2
br(audit9, 999, [(U475, "LOSS", -141)])
check("dedupe persists across AuditLog instances (daemon restart)",
      len(posts9) == 0, repr(posts9))

# ── 14: sanity — formatted bet text is what anchors the identity ─────────────

check("_format_pick canonicalizes both copies to one bet text",
      _format_pick(U475) == _format_pick(dict(U475)) and "47.5" in _format_pick(U475),
      _format_pick(U475))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
