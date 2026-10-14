"""Regression test: a same-game parlay never shows the product of its legs.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_same_game_parlay_price.py

Wager Stalk (-1004427337587:501, 2026-10-13) posted "SGP: BTTS + Barca ML +100"
on Galatasaray–Barcelona. Both leg prices were right (Pinnacle BTTS Yes -242,
DraftKings Barcelona -380), but every renderer multiplied them as independent
bets into [-127]. The legs are correlated: Pinnacle's own "BTTS & Barcelona"
special was -113 and the capper got +100. The pre-publish review held it. Once
released, the tag would have landed on "1.25U Barcelona vs Galatasaray:" right
above the capper's +100, and the broadcast and Sheets would show the same -127.

common.ticket_combined_odds is the one rule. Legs naming an overlapping team set
(same game, any period) have no honest product, so the ticket shows no price.
_insert_odds, the broadcast, Sheets and the odds gate all go through it, and the
odds-watch `parlay_same_game` check is gone with nothing left to flag.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds_checks as oc  # noqa: E402
import odds_gate as og  # noqa: E402
from audit import AuditLog  # noqa: E402
from common import parlay_combined_odds, same_game_leg_pairs, ticket_combined_odds  # noqa: E402
from tracker_format import _insert_odds  # noqa: E402

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  -> {detail}"))
    if not cond:
        failures.append(name)


# The incident's parse (parse_cache -1004427337587:501), verbatim.
PICKS = [
    {"description": "Sacramento Kings ML", "sport": None, "bet_type": "moneyline",
     "is_parlay_leg": False, "period": "game", "teams": ["Sacramento Kings"],
     "player": None, "prop_stat": None, "line": None, "direction": None},
    {"description": "Barcelona vs Galatasaray BTTS (SGP leg)", "sport": "Soccer",
     "bet_type": "prop", "is_parlay_leg": True, "period": "game",
     "teams": ["FC Barcelona", "Galatasaray"], "player": None, "prop_stat": "BTTS",
     "line": None, "direction": None},
    {"description": "FC Barcelona ML +100 (SGP leg)", "sport": "Soccer",
     "bet_type": "moneyline", "is_parlay_leg": True, "period": "game",
     "teams": ["FC Barcelona"], "player": None, "prop_stat": None, "line": None,
     "direction": None},
]
ODDS = {"0": {"odds": -102, "match_type": "exact", "bookmaker": "espn_draftkings"},
        "1": {"odds": -242, "match_type": "pinnacle_btts", "bookmaker": "pinnacle"},
        "2": {"odds": -380, "match_type": "exact", "bookmaker": "espn_draftkings"}}
RAW = "Wager Stalk 💎 \n\n1U Kings ML\n\n1.25U Barcelona vs Galatasaray:\nSGP: BTTS + Barca ML +100"

# ── the shared rule ──────────────────────────────────────────────────────────
legs = [PICKS[1], PICKS[2]]
check("BTTS + Barca ML is a same-game pair", same_game_leg_pairs(legs) == [(0, 1)],
      same_game_leg_pairs(legs))
check("same-game ticket has no combined price", ticket_combined_odds(legs, [-242, -380]) is None)
check("the old product was the incident's -127", parlay_combined_odds([-242, -380]) == -127)
a = {"teams": ["Los Angeles Chargers"]}
b = {"teams": ["Los Angeles Rams"]}
check("different games still multiply", ticket_combined_odds([a, b], [-150, -150])
      == parlay_combined_odds([-150, -150]))
half = {"teams": ["Kansas City Chiefs"], "period": "1h"}
full = {"teams": ["Kansas City Chiefs", "Denver Broncos"], "period": "game"}
check("same game across periods is still correlated", ticket_combined_odds([half, full], [-120, -110]) is None)
check("team-less legs (props) keep the product",
      ticket_combined_odds([{"teams": []}, {"teams": None}], [-110, -110]) == 264)

# ── the tag on the post ──────────────────────────────────────────────────────
out = _insert_odds(RAW, PICKS, ODDS)
check("post: standalone Kings tag still lands", "1U Kings ML \u2060-102" in out, out)
check("post: no multiplied ticket tag", "-127" not in out and "Galatasaray: \u2060" not in out, out)
check("post: capper's own +100 untouched", out.endswith("SGP: BTTS + Barca ML +100"), out)
check("post: idempotent", _insert_odds(out, PICKS, ODDS) == out)
cross = [dict(PICKS[2], description="Barcelona ML"), dict(PICKS[2], description="Arsenal ML",
                                                         teams=["Arsenal"])]
xout = _insert_odds("Parlay:\nBarcelona ML\nArsenal ML", cross,
                    {"0": {"odds": -380}, "1": {"odds": -200}})
check("post: a cross-game parlay keeps its combined tag",
      xout.startswith(f"Parlay: \u2060{parlay_combined_odds([-380, -200])}"), xout)

# ── the broadcast ────────────────────────────────────────────────────────────


def render(pick_results) -> list[str]:
    audit = AuditLog(db_path=str(Path(tempfile.mkdtemp()) / "t.db"), bot_token="T",
                     broadcast_results_mappings={-1001: -2001})
    posts: list[str] = []

    async def fake_post(*, target, text, reply_to_id, link):
        posts.append(text)

    audit._post_broadcast = fake_post
    asyncio.run(audit.broadcast_results(channel_id=-1001, message_id=501,
                                        pick_results=pick_results, capper_name="Wager Stalk"))
    return posts


posts = render([(PICKS[1], "WIN", -242), (PICKS[2], "WIN", -380)])
t = posts[0] if posts else ""
check("broadcast: ticket line posts", t.startswith("✅ Parlay:"), repr(posts))
check("broadcast: no multiplied price", "-127" not in t, t)

# ── the gate and the free check ──────────────────────────────────────────────
import json  # noqa: E402

body = json.loads(og.review_payload(PICKS, ODDS, RAW, {}))
check("gate payload: no product for a same-game ticket", body.get("parlay_combined_price") is None, body)
entry = {"html_text": RAW, "parsed": {"picks": PICKS}, "odds_by_pick": ODDS}
rules = sorted({h["rule"] for h in oc.scan_entry("-100:501", entry)})
check("free checks: nothing to flag", rules == [], rules)

print(f"\n{'ALL OK' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
