# Odds integration & Pikkit splits

> Full reference moved out of CLAUDE.md (terse rules live there). This file is read on demand — keep the complete detail and incident history HERE, not in CLAUDE.md.

## Odds integration

To force a re-fetch after manually restoring a cache entry: delete the `odds_by_pick` key from the relevant `parse_cache.json` entry — the next run will re-fetch and re-edit.

> ⚠️ **Only safe before first pitch.** The tracker fetches via `fetch_odds_current`, so a re-fetch after the game starts returns **live** prices instead of the pregame lines. To repair odds on a game already underway, write the values in directly: pull the closing lines with `odds._try_pregame(...)` against the correct event id and set `game_date` yourself. Cross-check by confirming a leg whose cached odds were already correct reproduces exactly.
>
> It used to be worse — the re-fetch could match the *next game in the series*, one day away, slipping under the `>2 days` wrong-game guard and making the bad `game_date` become `eff_date`, which pointed grading at the wrong game. That is fixed: `_find_event_id` no longer treats "already kicked off" as strictly worse than any future game (`_STARTED_GRACE_H`), so a game in progress beats the same matchup tomorrow. See the event-matching note below.

**A pick binds to an event by team name, and an absent game binds to the wrong one.** `_find_event_id` (`odds.py`) scores every event sharing a team name and returns the best — it never asks whether that event is plausibly the pick's game. So when a league's games are *missing* from the sport key we query, the pick doesn't miss: it silently prices off the same team's nearest listed game. The Odds API files preseason under separate keys, which is how a Panthers/Cardinals **preseason** total got Bears @ Panthers' Week 1 price (+320 against a real ~-125) and a Cardinals preseason ML got +455.

Three things keep that closed, all in `_find_event_id`/`fetch_odds*`:
- **`_EXTRA_SPORT_KEYS`** — extra keys carrying the same league in another phase (`americanfootball_nfl_preseason`, NBA/NHL preseason, MLB spring training). Event lists from every candidate key are merged and scored together and each event's own `sport_key` is used for the odds call. **Adding a league phase = one line here.** Out of season a key returns HTTP 200 + `[]`, and `/events` costs no quota (`x-requests-last: 0`), so carrying an unused key is free.
- **`as_of`** — events are ranked against the date the pick is *about*, not `datetime.now()`. Ranking by date rather than clamping to a window is deliberate: a UFC card is routinely posted five days early, so any fixed window breaks it.
- **Fail-closed on partial team matches** — a pick naming two teams that must be opponents rejects an event holding only one of them, *but only when the other is demonstrably on the schedule elsewhere*; a name variant `_team_matches` can't resolve must still fall through, or this drops odds we get right today.

Don't "fix" a wrong price by widening a guard downstream: the `>2 days` guard in `tracker.py` already fired here and the historical fallback re-ran the same matcher, re-matched the same game, and returned the identical price — with no `game_date` to show for it. `scripts/test_event_match_regression.py` pins all of this offline.

**Bovada is the free odds fallback for every major sport (`_BOVADA_PATHS` in `odds.py`: CFL, MLB, NFL incl. preseason, NCAAF, NBA, WNBA, NCAAB, NHL, UFC, UFL) — fallback only, the Odds API stays primary and ESPN is still tried first for its leagues.** Born when the quota ran out on 2026-08-08 and every CFL pick recorded `no_game`: the free `/events` endpoint still matched the game (which is why those entries carry a correct `game_date`) while the market call 401'd into an empty bookmakers list — and ESPN has no CFL at all (its empty CFL scoreboard is load-bearing for `validate_sport`), no period markets and no alternates anywhere. `_fetch_bovada_bookmakers` reads Bovada's public coupon JSON (no auth, no quota, works from the VPS; Lacrosse/PLL and KBO are NOT on Bovada — probed 404/empty 2026-08-22, so those two still have no free source) and shapes it into an Odds-API bookmakers list, so `lookup_pick_odds`, the proximity/gap guards and `_betonline_both_sides` (Bovada is already in `_BOOK_PRIORITY`) consume it unchanged. Shaping rules, each load-bearing:
- **Group allowlist (`_BOVADA_LINE_GROUPS`: Game Lines / Alternate Lines / Fight Odds) is the structural guard against props and exotics** — "Total Strikeouts - Logan Henderson (MIL)" reads exactly like a team total to any description rule but lives in Pitcher Props, and Game Props' "3-Way Moneyline" would land in h2h. Market titles then vary by sport and are all mapped: spread = "Point Spread"/"Runline"/"Puckline"/alternate "Spread", total = "Total"/"Total Runs O/U"/UFC's "Main Total Rounds Over/Under", a `" - <Team>"` suffix on a total marks a team total (team goes in outcome `description`, matching the API's team_totals shape), UFC's moneyline is "Fight Winner" at period B (bout).
- **Alternate lines fold into the main market key** (`spreads_h1`, not `alternate_spreads_h1` — `_lookup_spread` only reads `alternate_*` where the Odds API sells it, so an API-style key would make Bovada's 1H alternates invisible; for a one-book source main-vs-alternate is a request artifact, not a fact about the price).
- **Bovada's `1H` on MLB means First 5 Innings** and must map to `*_1st_5_innings` (`_bovada_suffix`, mirroring `_MLB_PERIOD_SUFFIX`), never `_h1`. NHL per-period abbreviations are deliberately unmapped until observed in a real payload (period markets only appear near game time; an unmapped period is skipped, a wrong mapping mislabels a market).
- **The pregame caller refuses a started event** (`_bovada_pick_event(allow_started=False)`) because post-kickoff the coupon serves live prices and nothing downstream could tell one recorded under a pregame match_type from a closing line — the started branch passes `allow_started=True` and gets the honest `live_` prefix, with `pregame_*` left None (Bovada has no history; near kickoff the coupon also trims to game lines only, so period markets are a pregame-window thing).
- **`_bovada_result_acceptable` is the cross-source same-game guard**: Bovada and the API match events independently, so their answers can be different games — seen live while testing: a Patriots pick whose actual game (PHI @ NE preseason, next day) is absent from Bovada's coupon matched Bovada's only listed Patriots event, Week 1 **18 days out**, and priced it while `game_date` said tomorrow. When the API matched a game Bovada must agree on the eastern date; when the API matched nothing, Bovada's game must be within `_BOVADA_MAX_DAYS_AHEAD` (7 — cappers post 0–5 days early), which keeps the genuine coverage win (a card the API doesn't list) while refusing a far-future stand-in.

Adding a league = one line in `_BOVADA_PATHS` plus a check of `_BOVADA_TEAM_ALIASES` (Bovada says "British Columbia Lions"; the parse and Odds API say "BC Lions" — `_team_matches` cannot bridge that pair). `scripts/test_bovada_fallback.py` pins the shaper, every market family and the guards offline against real captured coupons (CFL, MLB, UFC).

**A line the pick matched but that already states the capper's price IS the pick's line — stop there.** `_insert_odds` (`tracker_format.py`) skips such a line so we don't restate a number the capper already showed, and `_place` returns `None` for it. That `None` means "keep looking" for a line carrying a *different* pick's tag, but applying it to the source-price case walks the loop past the bet and onto whatever later line also matches — the write-up that names the team again, or an angle record holding the bet's number ("9-3 off 2 wins" for an over 9). The price then renders after the analysis instead of on the bet. Any such line resolves through the `src_declined` rule instead: quiet inside the 3% move threshold, `[-190 now]` on the pick line beyond it. `scripts/test_source_priced_odds.py` pins both halves offline (fall-through *and* later-line match). Note the sibling trap when repairing these live: the tracker rebuilds each edit from the current message text, so a tag on the wrong line must be **stripped**, not just re-placed.

**Blockquotes are angle records, never the bet — no placement pass may tag one, and the two matchers must not drift.** Second entry into the same landing spot as above, from the *unmatched* side: "Dbacks ML" parses to "Arizona Diamondbacks" (no word shared with the message), a bare ML has no line number, and stripping team words from the description leaves just "ml", so every `_insert_odds` pass failed and the pick fell to `_best_content_line`, whose last-content-line rule put `[-141]` on the "35-10 off 1 loss" blockquote — three times before it was caught (3639, its fan-out sibling, and 3485 a month earlier, deleted at source before anyone saw it). The emoji matcher (`_match_pick_line`) was immune all along: it excludes blockquote lines and carries a bet-line-heuristic pass (`\bml\b`, odds/spread/total shapes) that `_insert_odds` lacked, so odds and emoji disagreed about which line is the pick. Both now share `_blockquote_lines()` and both cascades end in the same heuristic — **when touching either matcher, change the other or make them share the code**. `scripts/test_odds_blockquote_placement.py` pins the class offline; for anything touching placement, the real regression net is the corpus replay (old-vs-new `_insert_odds` over every priced `parse_cache` entry with tags stripped) — it must show only the diffs you intended.

**Backtest / audit:**
```bash
python scripts/audit_odds.py --days-back 7
python scripts/audit_odds.py --dry-run
```

## Pikkit betting splits

`pikkit.py` fetches community betting splits (bet% + handle%) from Pikkit's API for each pick. Tracker calls `get_pick_splits()` after odds fetch and stores results in `pikkit_by_pick` in parse_cache. The dashboard shows a Book Interest column/filter based on this data.

- **API:** `prod-website.pikkit.app` — `/events/all` (event discovery) + `/event/foryou/{id}` (splits by market)
- **Auth:** `PIKKIT_TOKEN` in `.env.local` (opaque hex session_id, 400-day expiry)
- **Key constraint:** completed games return 403 — splits must be fetched before/during the game
- **Token generation:** `scripts/pikkit_auth.py` — must run locally (real Chrome + display), NOT on VPS. Turnstile rejects headless/bundled Chromium. Session is NOT IP-bound (tested 2026-07-24).

**Two-step manual auth flow (run locally):**
```bash
python scripts/pikkit_auth.py --send-sms --phone +19545361686
# Read SMS code, then:
python scripts/pikkit_auth.py --submit-code <CODE> --auth-id <from step 1> --phone +19545361686
```
Token is saved to `.env.local`. SCP to VPS or update `.env.local` there manually. `PIKKIT_TOKEN` must be in `.env.local`, NOT `.env` — `syncenv` would wipe it.

**Token health check (from VPS or locally):**
```bash
python scripts/pikkit_auth.py --validate
```

