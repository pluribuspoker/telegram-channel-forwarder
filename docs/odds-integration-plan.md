# Odds Integration — Plan & Current State

## Goal

Add odds to pick result broadcasts. Primary use-case: show the odds on each pick in the
broadcast result message (e.g. `✅ Capper · Duke -4.5 (-153)`).
Secondary: capture odds at message-send time in the listener (future).

---

## What Has Been Built

### `scripts/audit_odds.py`

Backtest/verification script. Reads `data/result.json` + `data/result_df.json`,
parses picks via Claude, fetches closing odds from Odds API, outputs `data/odds_audit.csv`.

**Key design:**
- Two-step Odds API fetch (unlocks alternate lines on existing paid plan):
  1. `GET /historical/sports/{sport}/events?date=…` — cheap event list, 1 quota
  2. `GET /historical/sports/{sport}/events/{id}/odds?markets=…` — full odds per event, ~10 quota
- Markets fetched per event: `h2h, spreads, totals, alternate_spreads, alternate_totals,
  h2h_h1, spreads_h1, totals_h1, h2h_h2, spreads_h2, totals_h2, h2h_q1, spreads_q1, totals_q1`
- ESPN fallback (`espn_bookmakers_for_teams` in `scores.py`) for pre-game picks where
  Odds API has no event; ESPN clears odds after game completion so this only fires live.
- Proximity matching up to 1.5 pts with half-point juice adjustment
  (`HALF_POINT_COST` per sport, ~0.022 implied-prob per half-pt)
- All Odds API responses cached to `data/odds_api_cache.json`
- Parse results cached to `data/audit_parse_cache.json`

**Current backtest results (109 picks):**
- 63 odds found (57%): 32 exact main line, 28 exact alt line, 3 proximity
- 20 unsupported sports (UFL/Tennis/Boxing — different data sources needed)
- 12 player props (separate endpoint needed)
- 9 no-game: small NCAAB schools + minor UFC cards not priced by Odds API
- 3 team totals (not in standard markets)
- 1 alt-line gap >1.5pts remaining

### `scores.py` additions

- `extract_espn_bookmaker(competition)` — converts ESPN `competition.odds[0]` to
  Odds-API-style bookmaker dict (h2h/spreads/totals, main lines only, no alternates)
- `espn_bookmakers_for_teams(espn_data, teams)` — finds event in ESPN scoreboard,
  returns bookmaker list (empty for completed games)
- `_QUALIFIERS` now includes `"st"` to prevent "Tennessee" matching "Tennessee St Tigers"

---

## Odds API Details

- **Key:** in `.env` as `ODDS_API_KEY`
- **Plan:** ~100k monthly quota (paid plan). Used ~49k so far this month.
- **Cost per backtest run:** ~300-500 quota (cached after first run — subsequent runs free)
- **Quota display:** `x-requests-remaining` header returned on every call
- **Sport keys:**

| Our name | Odds API key |
|---|---|
| NBA | `basketball_nba` |
| NCAAB | `basketball_ncaab` |
| NFL | `americanfootball_nfl` |
| NCAAF | `americanfootball_ncaaf` |
| MLB | `baseball_mlb` |
| NHL | `icehockey_nhl` |
| UFC | `mma_mixed_martial_arts` |

---

## Next Steps (in order)

### 1. Cost tracking: Odds API quota in run output (next item from `next` file)

Print Odds API quota used per run (like Claude cost). Both costs should be printed
clearly labelled after each tracker/grader run. Suggested approach: wrap the API call
method so quota delta is tracked centrally, same pattern as `_accum()` for Claude tokens.

### 2. Build `odds.py` module

Standalone module with entry point:
```python
async def fetch_odds(sport: str, game_date: str, pick: dict) -> int | None
```
Returns American odds integer or `None`. Reusable from both tracker and listener.

Internally uses the same two-step per-event approach from `audit_odds.py`.
Caches to `picks.db` table `odds_cache` (see schema below).

### 3. `odds_cache` table in `picks.db`

```sql
CREATE TABLE odds_cache (
    sport       TEXT NOT NULL,
    game_date   TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    market      TEXT NOT NULL,   -- 'spreads', 'alternate_spreads', 'h2h', etc.
    team        TEXT NOT NULL,
    line        REAL,            -- NULL for moneyline
    odds        INTEGER NOT NULL,
    bookmaker   TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (event_id, market, team, line)
);
```

Eviction: `DELETE FROM odds_cache WHERE game_date < date('now', '-60 days')` on each run.

### 4. Wire odds into tracker grading loop

After `claude_grade()` returns a verdict, call `fetch_odds(sport, game_date, pick)`.
Store in `grades.odds` column (add migration).

### 5. Show odds in broadcast

In `audit.py` `broadcast_results()` / `_format_pick()`, append odds to each pick line:
```
✅ Capper · Duke -4.5 (-153)
✅ Capper · Celtics ML (-130)
✅ Capper · Heat/Pistons O221.5 (-108)
```
Show nothing if `odds IS NULL` (graceful degradation).

---

## Open Questions / Known Gaps

- **UFL, Tennis, Boxing**: no Odds API coverage. UFL might be on a different provider.
  Tennis and boxing need separate data sources (investigate later).
- **Player props**: separate Odds API endpoint (`player_points_over_under` etc.) — not yet
  implemented. Quota cost is higher; defer until after main spread/total/ML coverage is solid.
- **Team totals**: not in standard Odds API markets; may need per-bookmaker scraping or skip.
- **Live bet odds**: picks labelled "live bet" have no pre-game line; always `no_odds`.
- **Pereira / Missouri matching anomalies**: occasionally wrong-game matched for fighters/teams
  with common surnames. Low priority; affects <5% of picks.
- **Odds at send time vs closing odds**: currently fetching closing (at grade time).
  The audit script infrastructure can be reused for send-time odds by calling the current
  (non-historical) endpoint when a pick arrives in `listener.py`.
