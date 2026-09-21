"""Regression test: a message mixing parlay legs with standalone picks.

Self-contained (no Telegram/API — the Bot API poster is stubbed) — run directly:

    ~/venv/bin/python scripts/test_mixed_parlay_broadcast.py

Incident 2026-09-20 (Midwest Mike, -1002486251914:3868): one message carried a
standalone total (Colts/Chiefs U47.5), a two-leg ML parlay (Chargers/Rams — Rams
play Monday), and a standalone ML (Broncos). Everywhere the code asked "is this
message a parlay?" it meant "does it CONTAIN parlay legs", so:

  * the Broncos WIN broadcast rendered the untouched ticket instead — the live
    "❓ … Parlay: Los Angeles Chargers ML / Los Angeles Rams ML" — and the
    Broncos ✅ never reached the results channel;
  * once the Chargers leg lost, `parlay_lost` voided EVERY unresolved leg,
    killing the standalone Colts/Chiefs pick whose game hadn't started;
  * in older entries (-1004339684312:217, -1004427337587:112) the inverse: a
    standalone leg's LOSS voided a live parlay that was never graded.

Pinned here: the ticket settles, voids, and renders on ITS OWN legs only.

  1. _parlay_lost: a parlay leg's LOSS kills the ticket
  2. _parlay_lost: a standalone leg's LOSS does NOT
  3. _void_moot_parlay_legs voids only the ticket's pending legs
  4. _ticket_settled: LOSS settles with a leg pending; WIN+PENDING doesn't;
     WIN+WIN settles even with a standalone sibling pending; VOID legs behave
  5. _bc_results_for: standalone-only newly → no ticket; parlay-leg newly +
     settled → whole ticket; unsettled ticket → nothing; non-parlay passthrough
  6. broadcast_results: standalone WIN renders the compact single line — no
     "Parlay:", no ❓ (the incident's exact payload)
  7. broadcast_results: pre-fix-shaped payload (all legs, ticket pending) still
     renders only the straight leg — defense in depth
  8. broadcast_results: settled ticket alone renders the classic compact
     "Parlay:" line, byte-format unchanged (incl. pushed-leg ♻️ + combined price)
  9. broadcast_results: straight WIN + settled ticket render as one multi-line
     message under the capper header
 10. broadcast_results: an unsettled ticket alone posts nothing
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grade_daemon as gd
from audit import AuditLog
from common import parlay_combined_odds

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  -> {detail}"))
    if not cond:
        failures.append(name)


# ── The incident's real picks (parse_cache -1002486251914:3868) ──────────────

def incident_picks() -> list[dict]:
    return [
        {"description": "Colts vs Chiefs Under 47.5 (-140)", "sport": "NFL",
         "bet_type": "total", "is_parlay_leg": False, "period": "game",
         "teams": ["Indianapolis Colts", "Kansas City Chiefs"],
         "line": 47.5, "direction": "under"},
        {"description": "Chargers ML parlay leg", "sport": "NFL",
         "bet_type": "moneyline", "is_parlay_leg": True, "period": "game",
         "teams": ["Los Angeles Chargers"], "line": None, "direction": None},
        {"description": "Rams ML parlay leg", "sport": "NFL",
         "bet_type": "moneyline", "is_parlay_leg": True, "period": "game",
         "teams": ["Los Angeles Rams"], "line": None, "direction": None},
        {"description": "Broncos ML -140", "sport": "NFL",
         "bet_type": "moneyline", "is_parlay_leg": False, "period": "game",
         "teams": ["Denver Broncos"], "line": None, "direction": None},
    ]


ODDS = {"0": {"odds": -141}, "1": {"odds": -325}, "2": {"odds": None}, "3": {"odds": -148}}


# ── 1+2: _parlay_lost scoping ────────────────────────────────────────────────

picks = incident_picks()
lv_chargers_lost = {"1": {"verdict": "LOSS"}, "3": {"verdict": "WIN"}}
check("parlay leg LOSS kills the ticket", gd._parlay_lost(picks, lv_chargers_lost) is True)

lv_standalone_lost = {"3": {"verdict": "LOSS"}}
check("standalone LOSS does NOT kill the ticket",
      gd._parlay_lost(picks, lv_standalone_lost) is False,
      "a lost straight pick voided a live parlay (entries 217/112)")

# ── 3: VOID scoped to ticket legs ────────────────────────────────────────────

lv = {"1": {"verdict": "LOSS", "broadcasted": True}, "3": {"verdict": "WIN", "broadcasted": True}}
dirty = gd._void_moot_parlay_legs(picks, lv, "NFL", "2026-09-20")
check("void marks the pending parlay leg", dirty and lv.get("2", {}).get("verdict") == "VOID",
      str(lv.get("2")))
check("void leaves the standalone pick alone", "0" not in lv,
      f"leg 0 (Colts/Chiefs U47.5, unplayed) got {lv.get('0')} — the incident's collateral")
check("void leaves resolved legs alone", lv["1"]["verdict"] == "LOSS" and lv["3"]["verdict"] == "WIN")

# ── 4: _ticket_settled ───────────────────────────────────────────────────────

check("LOSS settles the ticket with a leg pending",
      gd._ticket_settled(picks, {"1": {"verdict": "LOSS"}}) is True)
check("one WIN leg does not settle the ticket",
      gd._ticket_settled(picks, {"1": {"verdict": "WIN"}}) is False)
check("WIN+WIN settles despite pending standalone siblings",
      gd._ticket_settled(picks, {"1": {"verdict": "WIN"}, "2": {"verdict": "WIN"}}) is True,
      "a decided ticket must not wait on an unrelated late game")
check("LOSS+VOID stays settled",
      gd._ticket_settled(picks, {"1": {"verdict": "LOSS"}, "2": {"verdict": "VOID"}}) is True)
check("no parlay legs -> not a ticket",
      gd._ticket_settled([{"is_parlay_leg": False}], {"0": {"verdict": "WIN"}}) is False)

# ── 5: _bc_results_for composition ───────────────────────────────────────────

lv = {"1": {"verdict": "LOSS"}, "3": {"verdict": "WIN"}}

bc = gd._bc_results_for(picks, [3], [(picks[3], "WIN", -148)], lv, ODDS, "NFL",
                        include_ticket=False)
check("standalone newly -> its own line, no ticket",
      bc == [(picks[3], "WIN", -148)], repr(bc))

bc = gd._bc_results_for(picks, [1], [(picks[1], "LOSS", -325)], lv, ODDS, "NFL",
                        include_ticket=True)
check("parlay-leg newly + settled -> the whole ticket, ticket legs only",
      [p["description"] for p, _, _ in bc] == ["Chargers ML parlay leg", "Rams ML parlay leg"]
      and bc[0][1] == "LOSS" and bc[1][1] == "PENDING", repr(bc))

bc = gd._bc_results_for(picks, [1], [(picks[1], "WIN", -325)], lv, ODDS, "NFL",
                        include_ticket=False)
check("parlay-leg newly + UNsettled ticket -> broadcast nothing", bc == [], repr(bc))

straight_only = [{"description": "A ML", "is_parlay_leg": False},
                 {"description": "B ML", "is_parlay_leg": False}]
nr = [(straight_only[0], "WIN", -110), (straight_only[1], "LOSS", +120)]
bc = gd._bc_results_for(straight_only, [0, 1], nr, {}, {}, "NFL", include_ticket=False)
check("non-parlay message passes through unchanged", bc == nr, repr(bc))

# ── 6-10: broadcast_results rendering ────────────────────────────────────────

def render(pick_results) -> list[str]:
    audit = AuditLog(
        db_path=str(Path(tempfile.mkdtemp()) / "test_audit.db"),
        bot_token="TESTTOKEN",
        broadcast_results_mappings={-1001: -2001},
    )
    posts: list[str] = []

    async def fake_post(*, target, text, reply_to_id, link):
        posts.append(text)

    audit._post_broadcast = fake_post
    asyncio.run(audit.broadcast_results(
        channel_id=-1001, message_id=3868, pick_results=pick_results,
        capper_name="Midwest Mike", reply_to_id=777,
    ))
    return posts


# 6. What the fixed daemon sends at 23:01 (Broncos WIN only):
posts = render([(picks[3], "WIN", -148)])
check("standalone WIN posts once", len(posts) == 1, repr(posts))
t = posts[0] if posts else ""
check("standalone WIN is the compact single line, bet before capper",
      t.startswith("✅ ") and " -148 · " in t
      and t.index("Broncos ML") < t.index("Midwest Mike"), t)
check("standalone WIN carries no ticket and no ❓",
      "Parlay" not in t and "❓" not in t, t)

# 7. Defense in depth: the PRE-fix daemon payload (every leg, ticket pending):
posts = render([
    (picks[0], "PENDING", -141), (picks[1], "PENDING", -325),
    (picks[2], "PENDING", None), (picks[3], "WIN", -148),
])
check("pre-fix-shaped payload still renders only the straight leg",
      len(posts) == 1 and "Parlay" not in posts[0] and "❓" not in posts[0]
      and posts[0].startswith("✅ "), repr(posts))

# 8. The settled ticket alone — classic compact line, format unchanged:
posts = render([(picks[1], "LOSS", -325), (picks[2], "PENDING", None)])
check("lost ticket renders the compact Parlay line", len(posts) == 1, repr(posts))
t = posts[0] if posts else ""
check("lost ticket line format",
      t.startswith("❌ Parlay: Chargers ML / Rams ML")
      and " · " in t and "Midwest Mike" in t, t)
check("unpriced leg -> no combined price", "-325" not in t, t)

pushed = [
    ({"description": "Ko ML", "bet_type": "moneyline", "is_parlay_leg": True,
      "teams": ["Ko"], "sport": "UFC"}, "WIN", -150),
    ({"description": "Duncan ML", "bet_type": "moneyline", "is_parlay_leg": True,
      "teams": ["Duncan"], "sport": "UFC"}, "PUSH", -110),
]
posts = render(pushed)
t = posts[0] if posts else ""
expected_price = parlay_combined_odds([-150])
check("WIN+PUSH ticket: ✅, inline ♻️, pushed leg dropped from the price",
      t.startswith("✅ ") and "♻️" in t and f" {expected_price} · " in t, t)

# 9. Straight WIN + settled ticket in one payload -> one multi-line message:
posts = render([
    (picks[3], "WIN", -148),
    (picks[1], "LOSS", -325), (picks[2], "PENDING", None),
])
check("mixed settled payload posts once", len(posts) == 1, repr(posts))
t = posts[0] if posts else ""
lines = t.split("\n")
check("mixed: capper header + straight line + ticket line",
      len(lines) == 3 and "Midwest Mike" in lines[0]
      and lines[1].startswith("✅ ") and lines[1].endswith(" -148")
      and lines[2].startswith("❌ Parlay: Chargers ML / Rams ML"), t)

# 10. An unsettled ticket alone posts nothing:
posts = render([(picks[1], "WIN", -325), (picks[2], "PENDING", None)])
check("unsettled ticket alone posts nothing", posts == [], repr(posts))

print()
if failures:
    print(f"❌ {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ all mixed-parlay broadcast cases pass")
