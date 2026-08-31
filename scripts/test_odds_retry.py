"""Regression test: API-last-resort source order + retryable-miss eligibility.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_odds_retry.py

The paid Odds API subscription ended 2026-08-22 and the key reverts to the
free 500-credits/month tier, so fetch_odds_current now tries the free sources
first (ESPN → Bovada) and only spends a paid market call when neither priced
the pick; free_only=True (the tracker's miss-retry loop) must never spend one.
Retries exist because Bovada's period markets (MLB F5, CFL quarters/halves)
appear on the coupon in a pregame window the first attempt can post hours
ahead of — should_retry_odds bounds them by commence_time/game_date with
30-min spacing, and anything priced/live/structural stays final.

The Bovada side reuses the real captured CFL coupon fixture (never retyped);
_fetch_bovada_bookmakers is stubbed at the seam because the fixture's game
(Winnipeg @ Edmonton, 2026-08-21) has long started and the pregame picker
would (correctly) refuse it — the picker itself is pinned by
scripts/test_bovada_fallback.py.
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds
from odds import (
    OddsResult,
    _bovada_bookmakers,
    fetch_odds_current,
    should_retry_odds,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bovada_cfl_pregame.json"
_ET = ZoneInfo("America/New_York")

failures = []


def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        failures.append(label)


# ── should_retry_odds truth table ─────────────────────────────────────────────

def test_should_retry():
    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_et = now.astimezone(_ET).date().isoformat()
    yesterday_et = (now.astimezone(_ET).date() - timedelta(days=1)).isoformat()

    check("priced result is final",
          should_retry_odds({"odds": -110, "match_type": "exact"}), False)
    check("no_game before commence retries",
          should_retry_odds({"odds": None, "match_type": "no_game", "commence_time": future}), True)
    check("no_game after commence is final",
          should_retry_odds({"odds": None, "match_type": "no_game", "commence_time": past}), False)
    check("F5 no-data before commence retries",
          should_retry_odds({"odds": None, "match_type": "no_h2h_1st_5_innings_data",
                             "commence_time": future}), True)
    check("no_spread_data on game day (date only) retries",
          should_retry_odds({"odds": None, "match_type": "no_spread_data",
                             "game_date": today_et}), True)
    check("no_spread_data past game day is final",
          should_retry_odds({"odds": None, "match_type": "no_spread_data",
                             "game_date": yesterday_et}), False)
    check("30-min spacing holds a fresh attempt",
          should_retry_odds({"odds": None, "match_type": "no_game", "commence_time": future,
                             "_retry_ts": time.time() - 60}), False)
    check("spacing elapsed retries again",
          should_retry_odds({"odds": None, "match_type": "no_game", "commence_time": future,
                             "_retry_ts": time.time() - 31 * 60}), True)
    check("retry cap is final",
          should_retry_odds({"odds": None, "match_type": "no_game", "commence_time": future,
                             "_retry_n": odds._ODDS_RETRY_MAX}), False)
    check("live_ result is final",
          should_retry_odds({"odds": None, "match_type": "live_no_game", "commence_time": future}), False)
    check("game_in_progress is final",
          should_retry_odds({"odds": None, "match_type": "game_in_progress",
                             "game_date": today_et}), False)
    check("structural prop miss is final",
          should_retry_odds({"odds": None, "match_type": "prop_stat_unsupported(NRFI)",
                             "commence_time": future}), False)
    check("no date info means no retry",
          should_retry_odds({"odds": None, "match_type": "no_game"}), False)


# ── Source order: free first, paid API last resort ────────────────────────────

class Stub:
    """Patches every network seam in fetch_odds_current and counts paid calls."""

    def __init__(self, *, commence_dt, bovada_bk, bovada_gd, paid_bk=None):
        self.paid_calls = 0
        self.pregame_calls = 0
        self.commence = commence_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.gd = commence_dt.astimezone(_ET).strftime("%Y-%m-%d")
        self.bovada = (bovada_bk, bovada_gd)
        self.paid_bk = paid_bk or []
        self.event = {"id": "ev1", "sport_key": "americanfootball_cfl",
                      "home_team": "Edmonton Elks", "away_team": "Winnipeg Blue Bombers",
                      "commence_time": self.commence}

    def install(self):
        async def _events(sport, conn):
            return [self.event]

        async def _bovada(sport, teams, allow_started=False):
            return self.bovada

        async def _espn(sport, date):
            return None

        async def _paid(sport_key, event_id, markets, conn, *, live=False):
            self.paid_calls += 1
            return self.paid_bk

        async def _pregame(sport, sport_key, event_list, event_id, pick, db_path):
            self.pregame_calls += 1
            return None

        self.saved = (odds._fetch_current_event_list_all, odds._fetch_bovada_bookmakers,
                      odds.fetch_espn, odds._fetch_current_bookmakers, odds._try_pregame)
        odds._fetch_current_event_list_all = _events
        odds._fetch_bovada_bookmakers = _bovada
        odds.fetch_espn = _espn
        odds._fetch_current_bookmakers = _paid
        odds._try_pregame = _pregame

    def restore(self):
        (odds._fetch_current_event_list_all, odds._fetch_bovada_bookmakers,
         odds.fetch_espn, odds._fetch_current_bookmakers, odds._try_pregame) = self.saved


def run(pick, stub, **kw):
    stub.install()
    try:
        return asyncio.run(fetch_odds_current("CFL", pick, db_path=":memory:", **kw))
    finally:
        stub.restore()


def test_source_order():
    event = json.load(open(FIXTURE))[0]["events"][0]
    bovada_bk = _bovada_bookmakers(event, "CFL")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    past = datetime.now(timezone.utc) - timedelta(hours=2)

    # The real pick that exposed the period-market gap: Blue Bombers 1H +3.5.
    pick_1h = {"bet_type": "spread", "teams": ["Winnipeg Blue Bombers"],
               "line": 3.5, "period": "1h", "description": "Blue Bombers 1H +3.5"}
    ml = {"bet_type": "moneyline", "teams": ["Winnipeg Blue Bombers"],
          "description": "Blue Bombers ML"}

    # A: Bovada prices the pick → the paid market call must never fire.
    stub = Stub(commence_dt=future, bovada_bk=bovada_bk, bovada_gd=None, paid_bk=[
        {"key": "stub", "title": "Stub",
         "markets": [{"key": "spreads_h1", "outcomes": [
             {"name": "Winnipeg Blue Bombers", "price": -200, "point": 3.5}]}]}])
    stub.bovada = (bovada_bk, stub.gd)
    r = run(pick_1h, stub)
    check("A: Bovada priced the 1H spread", r.odds, -105)
    check("A: bookmaker is bovada", r.bookmaker, "bovada")
    check("A: paid market call skipped", stub.paid_calls, 0)
    check("A: commence_time recorded", r.commence_time, stub.commence)
    check("A: game_date recorded", r.game_date, stub.gd)

    # B: free_only NEVER spends a paid call, even when the paid side could price.
    stub = Stub(commence_dt=future, bovada_bk=[], bovada_gd=None, paid_bk=[
        {"key": "stub", "title": "Stub",
         "markets": [{"key": "h2h", "outcomes": [
             {"name": "Winnipeg Blue Bombers", "price": 150},
             {"name": "Edmonton Elks", "price": -170}]}]}])
    r = run(ml, stub, free_only=True)
    check("B: free_only finds no price", r.odds, None)
    check("B: free_only spends nothing", stub.paid_calls, 0)

    # C: free sources miss → the paid API is the last resort and prices it.
    stub = Stub(commence_dt=future, bovada_bk=[], bovada_gd=None, paid_bk=[
        {"key": "stub", "title": "Stub",
         "markets": [{"key": "h2h", "outcomes": [
             {"name": "Winnipeg Blue Bombers", "price": 150},
             {"name": "Edmonton Elks", "price": -170}]}]}])
    r = run(ml, stub)
    check("C: paid last resort priced", r.odds, 150)
    check("C: exactly one paid call", stub.paid_calls, 1)

    # D: started game on a free-only retry → Bovada live only, no paid calls,
    # no historical closing-line call.
    stub = Stub(commence_dt=past, bovada_bk=bovada_bk, bovada_gd=None)
    stub.bovada = (bovada_bk, stub.gd)
    r = run(ml, stub, free_only=True)
    check("D: started retry stays free (no paid calls)", stub.paid_calls, 0)
    check("D: started retry skips historical closing", stub.pregame_calls, 0)
    check("D: live price carries the live_ prefix", (r.match_type or "").startswith("live_"), True)

    # E: started game, first encounter (paid allowed): Bovada live prices it,
    # so the only paid spend is the closing-line attempt.
    stub = Stub(commence_dt=past, bovada_bk=bovada_bk, bovada_gd=None)
    stub.bovada = (bovada_bk, stub.gd)
    r = run(ml, stub)
    check("E: Bovada live beats the paid live call", stub.paid_calls, 0)
    check("E: closing line still attempted once", stub.pregame_calls, 1)


def main():
    test_should_retry()
    test_source_order()
    print()
    if failures:
        print(f"❌ {len(failures)} failure(s): {failures}")
        sys.exit(1)
    print("✅ all checks passed")


if __name__ == "__main__":
    main()
