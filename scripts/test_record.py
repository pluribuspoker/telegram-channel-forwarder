"""scripts/record.py — the bet-by-bet record message. Offline, no Telegram.

    ~/venv/bin/python scripts/test_record.py

Pins: payout/ROI math, the sides/totals filter, Telegram-size splitting at
group boundaries, and the capper source (grades rows → one bet per leg; the
structured parse wins where the cache still has it; parlay legs, voids and
fanned-out copies drop out; unrecognised names don't match).
"""
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import record as rec

ET = rec.ET


def _bet(day, result, kind="total", units=1.0, price=-110, group="Week 1", bet="o45"):
    return {"when": datetime(2026, 9, day, 13, tzinfo=ET), "group": group, "game": "KC@MIA",
            "bet": bet, "price": price, "units": units, "result": result, "kind": kind, "final": ""}


def test_payout_and_header():
    bets = [_bet(13, "W", units=1.1, price=-110), _bet(14, "L", units=1.8),
            _bet(15, "P"), _bet(16, "W", kind="side", price=100, units=0.7, bet="JAX +2.5")]
    assert round(rec.payout(bets[0]), 2) == 1.0
    assert rec.payout(bets[1]) == -1.8 and rec.payout(bets[2]) == 0.0
    assert rec.record(bets) == "2-1-1"
    msg = rec.render("👑 GOD JUDGE · NFL", bets)[0]
    assert "<b>2-1-1</b>" in msg and "-0.10u" in msg and "ROI -2.2%" in msg, msg
    assert "Sides 1-0" in msg and "Totals 1-1-1" in msg and "1.8u bets: 0-1" in msg
    assert "Streak: W1" in msg and "✅❌♻️✅" in msg


def test_market_filter():
    bets = [_bet(13, "W"), _bet(14, "L", kind="side", bet="CHI -3.5"), _bet(15, "W", kind="other")]
    assert [b["kind"] for b in rec.filter_market(bets, "sides")] == ["side"]
    assert [b["kind"] for b in rec.filter_market(bets, "totals")] == ["total"]
    msg = rec.render("X", rec.filter_market(bets, "sides"), "sides")[0]
    assert "· SIDES" in msg and "CHI -3.5" in msg and "o45" not in msg
    assert "No graded bets yet." in rec.render("X", [], "totals")[0]


def test_split_at_group_boundaries():
    bets = [_bet(1 + i % 28, "W", group=f"Week {i // 10}") for i in range(400)]
    msgs = rec.render("X", bets)
    assert len(msgs) > 1 and all(len(m) <= rec.MAX_MSG for m in msgs)
    for m in msgs:  # a table never straddles two messages
        assert m.count("<pre>") == m.count("</pre>")
    assert "(cont.)" in msgs[1]


def test_capper_source(tmp):
    db = Path(tmp) / "picks.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE grades (channel_id INT, message_id INT, date TEXT, capper_name TEXT, "
                "bet_type TEXT, verdict TEXT, odds INT, pick_desc TEXT, graded_at TEXT, dry_run INT)")
    rows = [
        # structured parse in the cache: the parlay leg drops, the straight stays
        (-100, 1, "2026-10-04", "◼️ Trent", "spread", "UNKNOWN", -110,
         "WIN: Chicago Bears -3.5|NFL|2026-10-04|x\nLOSS: Jets ML (parlay leg)|NFL|2026-10-04|y", "a"),
        # no cache entry: leg text only, price from the text, kind by heuristic
        (-100, 2, "2026-10-05", "Trent", "moneyline", "WIN", None,
         "WIN: Boston Red Sox moneyline (-107)|MLB|2026-10-05|[final] 5 vs 3", "b"),
        (-100, 3, "2026-10-05", "trent", "total", "LOSS", -105,
         "LOSS: Rangers vs Mariners Under 7.5|MLB|2026-10-05|calc\nwith a second line", "c"),
        # the same pick fanned out to a second channel → counted once
        (-200, 9, "2026-10-05", "◼️ Trent", "moneyline", "WIN", None,
         "WIN: Boston Red Sox moneyline (-107)|MLB|2026-10-05|[final] 5 vs 3", "d"),
        (-100, 4, "2026-10-06", "Trent", "moneyline", "UNKNOWN", None,
         "VOID: Cubs ML|MLB|2026-10-06|x", "e"),
        (-100, 5, "2026-10-06", "Travy", "moneyline", "WIN", -120, "WIN: Cubs ML|MLB|2026-10-06|x", "f"),
    ]
    con.executemany("INSERT INTO grades VALUES (?,?,?,?,?,?,?,?,?,0)", rows)
    con.commit()
    con.close()
    cache = Path(tmp) / "parse_cache.json"
    cache.write_text(json.dumps({"-100:1": {
        "parsed": {"picks": [
            {"description": "Chicago Bears -3.5", "bet_type": "spread", "teams": ["Chicago Bears"],
             "line": -3.5, "period": "game", "is_parlay_leg": False, "sport": "NFL"},
            {"description": "Jets ML", "bet_type": "moneyline", "teams": ["New York Jets"],
             "period": "game", "is_parlay_leg": True, "sport": "NFL"}]},
        "odds_by_pick": {"0": {"odds": -113}}}}))
    names = rec.capper_names(db)
    # Spellings merge; Travy's single row is under MIN_CAPPER_PICKS (junk-header filter).
    assert names == {"trent": {"◼️ Trent", "Trent", "trent"}}, names
    bets = rec.capper_bets(names["trent"], db, cache)
    got = [(b["bet"], b["price"], b["result"], b["kind"]) for b in bets]
    assert got == [("Bears -3.5", -113, "W", "side"), ("Red Sox ML", -107, "W", "side"),
                   ("Rangers/Mariners u7.5", -105, "L", "total")], got
    assert all(b["units"] == 1.0 for b in bets)


def main():
    tests = [test_payout_and_header, test_market_filter, test_split_at_group_boundaries, test_capper_source]
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
