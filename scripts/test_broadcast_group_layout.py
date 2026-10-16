"""Regression test: score-header result layout (operator 2026-10-16).

Self-contained (Bot API poster stubbed) — run directly:

    ~/venv/bin/python scripts/test_broadcast_group_layout.py

The Seahawks 14–17 Broncos merge read "Seahawks/Broncos U41.5" under a header
that already names the game, interleaved sides with totals, and wrapped
"RIGGED / GEMS" and a stranded "· Boyer" onto their own lines. Pinned:

  1. under a score header, game totals and team-level props drop the matchup
     and totals spell out Over/Under ("Under 41.5");
     team totals keep their team; headerless merges keep the matchup
  2. lines order sides → totals → props, message order within each
  3. capper names and the "· name" join use non-breaking spaces (both senders)
  4. 2+ bets all won → 🧹🧹🧹 after the header, all lost → 🥶🥶🥶; mixed, a push, a
     single bet, or a headerless merge → no icon, never tally text
  5. the dedupe fingerprint keeps the full label (no broadcast_lines re-seed)
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit import AuditLog, _format_pick

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  -> {detail}"))
    if not cond:
        failures.append(name)


NB = " "
TEAMS = ["Seattle Seahawks", "Denver Broncos"]


def pk(bet_type, teams=TEAMS, **kw):
    base = {"sport": "NFL", "bet_type": bet_type, "teams": teams, "period": "game",
            "player": None, "prop_stat": None, "line": None, "direction": None,
            "is_parlay_leg": False, "description": ""}
    base.update(kw)
    return base


SPREAD = pk("spread", teams=["Denver Broncos"], line=3)
U415 = pk("total", line=41.5, direction="under")
ML = pk("moneyline", teams=["Denver Broncos"])
U405 = pk("total", line=40.5, direction="under")
TT = pk("team_total", teams=["Denver Broncos"], line=20.5, direction="over")
H1 = pk("total", line=20.5, direction="under", period="1h")
BTTS = pk("prop", teams=["Arsenal", "Chelsea"], prop_stat="BTTS", direction="over", sport="Soccer")


def run(items, header="🏈 Seahawks 14–17 Broncos", sender="group"):
    posts = []
    audit = AuditLog(db_path=str(Path(tempfile.mkdtemp()) / "a.db"), bot_token="T",
                     broadcast_results_mappings={-1001: -2001})

    async def fake_post(*, target, text, reply_to_id, link):
        posts.append(text)

    audit._post_broadcast = fake_post
    if sender == "group":
        asyncio.run(audit.broadcast_group(target_channel=-2001, header=header,
                                          items=items, reply_to_id=None))
    else:
        asyncio.run(audit.broadcast_results(channel_id=-1001, message_id=1,
                                            pick_results=items, capper_name=header,
                                            reply_to_id=1))
    return posts[0] if posts else ""


def it(pick, verdict, odds, capper, msg_id):
    return {"channel_id": -1001, "message_id": msg_id, "capper": capper,
            "pick": pick, "verdict": verdict, "odds": odds}


# ── The 2026-10-16 post, byte-for-byte input order ──────────────────────────
ITEMS = [it(SPREAD, "WIN", -125, "RIGGED GEMS", 1), it(U415, "WIN", -123, "Boyer", 2),
         it(ML, "WIN", 124, "Dagger", 3), it(U405, "WIN", -110, "Dagger", 3)]
text = run(ITEMS)
lines = text.split("\n")
bets = [l.split(" ·")[0] for l in lines[2:]]
check("1 totals drop the matchup under the header",
      bets == ["✅ Broncos +3 -125", "✅ Broncos ML +124", "✅ Under 41.5 -123", "✅ Under 40.5 -110"], repr(bets))
check("2 sides before totals, message order kept", bets[0].startswith("✅ Broncos +3"), repr(bets))
check("3 capper name + middot join are non-breaking",
      f"·{NB}<a" in lines[2] and f"RIGGED{NB}GEMS" in lines[2] and "· <a" not in text, repr(lines[2]))
check("4 clean sweep → 🧹🧹🧹 after the header", lines[0].endswith("</u></b> 🧹🧹🧹"), repr(lines[0]))

# Team total keeps its team; 1H keeps its period; team-level prop drops the matchup
check("team total keeps its team", _format_pick(TT, in_game=True) == "Broncos Over 20.5",
      _format_pick(TT, in_game=True))
check("period total keeps the period", _format_pick(H1, in_game=True) == "1H Under 20.5",
      _format_pick(H1, in_game=True))
check("BTTS drops the matchup in-game", _format_pick(BTTS, in_game=True) == "BTTS Yes",
      _format_pick(BTTS, in_game=True))
check("full label unchanged (fingerprints, Sheets)",
      _format_pick(U415) == "Seahawks/Broncos U41.5" and _format_pick(BTTS) == "Arsenal vs Chelsea BTTS Yes",
      _format_pick(U415))

# Headerless merge keeps the matchup and gets no icon
t2 = run(ITEMS, header="")
check("headerless merge keeps the matchup, no icon",
      "Seahawks/Broncos U41.5" in t2 and "🧹" not in t2 and "🥶" not in t2, repr(t2))

# Reverse sweep / mixed / push / single
t3 = run([it(SPREAD, "LOSS", -125, "A", 1), it(U415, "LOSS", -110, "B", 2)])
check("reverse sweep → 🥶🥶🥶", t3.split("\n")[0].endswith(" 🥶🥶🥶"), repr(t3))
t4 = run([it(SPREAD, "WIN", -125, "A", 1), it(U415, "LOSS", -110, "B", 2)])
check("mixed → no icon", t4.split("\n")[0].endswith("</u></b>"), repr(t4))
t5 = run([it(SPREAD, "WIN", -125, "A", 1), it(U415, "PUSH", -110, "B", 2)])
check("a push breaks the sweep", t5.split("\n")[0].endswith("</u></b>"), repr(t5))
t6 = run([it(SPREAD, "WIN", -125, "A", 1), it(SPREAD, "WIN", -125, "B", 2)])
check("one bet merged across cappers is not a sweep", t6.split("\n")[0].endswith("</u></b>"), repr(t6))

# Compact sender: non-breaking capper tail too
t7 = run([(U415, "WIN", -110)], header="RIGGED GEMS", sender="results")
check("compact line keeps the matchup, non-breaking tail",
      t7.startswith("✅ Seahawks/Broncos U41.5 -110 ·" + NB) and f"RIGGED{NB}GEMS" in t7, repr(t7))

print(f"\n{len(failures)} failure(s)" if failures else "\nall passed")
sys.exit(1 if failures else 0)
