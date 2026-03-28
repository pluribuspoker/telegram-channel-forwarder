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

## What Has Been Built (continued)

### `odds.py`

Production module. Key exports:
- `fetch_odds(sport, game_date, pick) -> OddsResult` — ESPN first, historical Odds API
  fallback. SQLite cache in `picks.db` (tables: `odds_cache`, `events_cache`).
- `OddsResult` — carries `match_type`, `odds`, `bookmaker`, `api_line`, `pick_line`.
  `.validate_for_display()` runs sanity checks before Telegram output.
  `.is_unexpected_miss` / `.is_structural_miss` for failure triage.
  `.format()` → `"-110"` / `"+150"`.
- `quota_used()` — Odds API units consumed this process.

`audit_odds.py` imports all matching logic from `odds.py` (no duplication).

---

## Next Steps (in order)

### 1. ~~Cost tracking~~ ✅ Done

### 2. ~~Build `odds.py` module~~ ✅ Done

### 3. Wire odds into tracker — fetch at first encounter, store in `picks.db`

**Design: lazy fetch at first encounter using current (live) endpoint.**

When the tracker first processes a PENDING pick (before it has a grade):
- Call `fetch_odds_current(sport, pick)` — uses the non-historical Odds API endpoint
  (`GET /sports/{sport}/events/{id}/odds`, no `date` param) to get live lines.
- This is close enough to send-time since the tracker runs every 5 min.
- Store the result in a new `grades.odds` column (add migration).
- On subsequent tracker runs, use the stored value — never re-fetch once set.

**`fetch_odds_current()` in `odds.py`:**
```python
async def fetch_odds_current(sport: str, pick: dict, db_path: str = DB_PATH) -> OddsResult
```
Same matching logic as `fetch_odds()` but hits the live endpoint. Cache key is separate
(`current_event_odds:{event_id}`) with a short TTL (e.g. evict after 2 days) since
live odds change.

**Migration:**
```sql
ALTER TABLE grades ADD COLUMN odds INTEGER;
ALTER TABLE grades ADD COLUMN odds_bookmaker TEXT;
ALTER TABLE grades ADD COLUMN odds_match_type TEXT;
```

**Failure handling:**
- `result.is_unexpected_miss` → print `[odds] unexpected miss: {match_type} — {desc}`,
  increment `errors` counter in tracker summary line.
- `result.is_structural_miss` → silent, store `odds=NULL`.
- Sanity check failure → store `odds=NULL`, print warning.
- Summary line gains `odds:X/Y` (found X out of Y picks attempted).

### 4. Show odds in broadcast

In `audit.py` `broadcast_results()` / `_format_pick()`, read `grades.odds` and append:
```
✅ Capper · Duke -4.5 (-153)
✅ Capper · Celtics ML (-130)
✅ Capper · Heat/Pistons O221.5 (-108)
```
Graceful degradation: show nothing if `odds IS NULL` or sanity check fails.

---

## Open Questions / Known Gaps

- **UFL, Tennis, Boxing**: UFL now covered. Tennis and boxing need separate data sources.
- **Player props**: supported in `odds.py` for MLB/NBA/NHL/NFL via separate prop market
  fetch. Quota cost is higher; covered at 78% on recent picks.
- **Team totals**: not in standard Odds API markets — always `NULL`.
- **Live bet odds**: picks labelled "live bet" have no pre-game line; always `NULL`.
- **MLB First 5 Innings ML**: `h2h_1st5` not returned by Odds API — always `NULL`.
- **Pereira / Missouri matching anomalies**: occasionally wrong-game matched for fighters
  with common surnames. Low priority, affects <5% of picks.
