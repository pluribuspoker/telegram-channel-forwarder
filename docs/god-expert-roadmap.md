# God Expert Roadmap — implementation plan

The executable version of the roadmap page (God Expert Roadmap artifact; the
Bake-off and Desk artifacts are its companions — URLs in the Claude memory
index). Work packages, dependencies, acceptance tests, and the orchestration
protocol. The living-plan rule from `docs/telegram-intake-plan.md` applies:
update the status log here as items land, and record any decision that
changes the plan next to the item it changes.

## Ground rules

These are settled. Do not relitigate them inside an implementation session.

- Exactly two God Experts: `god_rules` (deterministic) and `god_judge`
  (Fable 5.1 through the agent-runtime skill). No third arm. A rating voice
  is a committee input, not an arm; a judge ensemble is still one judge.
- One `aggregator_policy` block in `moe/experts.yaml`, shared by both arms.
  No policy versioning. Changes land between weeks, never with games in
  progress; each row already records the knobs it ran under, so the ledger
  stays readable by date.
- Numbers settle by arithmetic. The judge returns only probabilities and
  reasons; `apply_policy` decides legs, stars, and stake for both arms.
- The judge never runs in a session that has seen unmasked committee rows.
  One fresh session per judge run: `--show-input` in, response out.
- Tests are Unix-only (`moe.py` imports `fcntl`). Run them on the VPS from a
  scratch clone, never in `~/app`:

  ```bash
  ssh root@209.38.51.86 'su - forwarder -c "git clone -q /home/forwarder/app /tmp/godbuild-<slug>"'
  # tar the changed files over the clone, strip CRs, chown forwarder, then:
  su - forwarder -c "cd /tmp/godbuild-<slug> && ~/venv/bin/python -m unittest scripts.test_moe_god scripts.test_moe scripts.test_moe_ak scripts.test_moe_win_total scripts.test_generate_moe_opinion_cli scripts.test_intake_bot"
  ```

  Each worktree uses its own scratch clone so parallel runs never collide.
- Deploy: commit on main → push → `git pull` **as root** in
  `/home/forwarder/app` (root owns files there; a pull as `forwarder` fails
  half-way and leaves a partial checkout) → `systemctl restart
  telegram-intake.service` only when `moe.py` or `intake_bot.py` changed.
- Historical lines come from free sources only: the nflverse games file and
  ESPN's core odds endpoint. Never the paid Odds API historical endpoint.

## Verified data sources (probed 2026-09-07)

- **nflverse games file** —
  `https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv`.
  Columns include `season, game_type, week, gameday, away_team, home_team,
  away_score, home_score, spread_line, total_line, away_moneyline,
  home_moneyline, away_spread_odds, home_spread_odds, over_odds, under_odds,
  espn`. Complete for every regular-season game in 2023, 2024, and 2025
  (272 each), and back to 1999 for spread and total. Teams are nflverse
  abbreviations (`LA`, `LV`, `KC`, …); build and assert a 32-team map to the
  full names used everywhere in this repo (`TEAM_ABBREVIATIONS` in
  `nfl_win_predictions.py` is full-name → abbreviation and its codes differ
  in places). Verify the sign convention of `spread_line` against a few
  known games before use.
- **ESPN core odds** —
  `https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{id}/competitions/{id}/odds`.
  Returns `items[]` per provider. For 2024 and 2025 games the `ESPN BET`
  item carries `open`, `close`, and `current` blocks (total line and juice
  in the top-level blocks; spread line and moneyline inside
  `homeTeamOdds`/`awayTeamOdds` per block). For 2023 games the endpoint
  lists thirteen providers with inconsistent opens; use closes only, or skip
  2023 for movement. The `details`/`spread` top-level fields and the
  per-team `pointSpread` did not obviously agree in one probe; cross-check
  signs against nflverse for the same games before trusting either.
- **ESPN game-summary `pickcenter`** (what `scores.espn_closing_odds` uses)
  holds roughly eight months and is empty for older games. Not a backtest
  source.
- The ESPN event id for a game is in nflverse's `espn` column, which is
  how the two sources join.

## Work packages

Days are build days for one agent, tests and docs included. No paid inference
anywhere; judge runs bill the Claude Code subscription.

### WP1 — Market-move veto and expected-value floor (0.5 d)

- `aggregator_policy` keys: `veto_adverse_spread_points` (0.5),
  `veto_adverse_total_points` (1.0), `veto_adverse_price_cents` (10),
  `min_ev_per_unit` (0.02).
- `moe_god.apply_policy` reads `market["movement_since_open"]`; add the
  per-side spread-price delta (opening vs latest price on the bet's side) to
  `build_market_block`. A leg whose line or price moved against it by at
  least the threshold since open passes with the note `adverse move`; a leg
  whose `ev_per_unit` is under the floor passes with `ev floor`.
- Tests in `scripts/test_moe_god.py` (`PolicyTests`), plus a Week 1 replay
  test: on the persisted Seahawks input the side passes on the price move
  and the Over passes on the floor; on the Rams input 49ers +3.5 still bets.
- Docs: intake plan section + `moe/prompts/god_rules/v2.md` is **not**
  needed — the spec file describes the algorithm generically; bump only if
  the wording there becomes wrong.

### WP2 — Judge plumbing (1 d)

- `--input-file <path>` on `scripts/generate_moe_opinion.py`, valid with
  `--agent-response` and `--deterministic`: the file's JSON becomes the
  persisted `input_json` (its hash must equal `--expected-input-sha256`
  when both are given); `generate_opinion` accepts a prebuilt payload. The
  judge stops racing the 30-minute lines fetcher; both arms can be pinned to
  one sheet state.
- Fresh-session runbook in `.claude/skills/generate-nfl-moe-opinion/SKILL.md`
  (and the `.github` copy if kept in sync): start a session that has not
  touched the sheet, run `--show-input`, give it only the prompt and the
  request, persist with `--agent-response --input-file`.
- Reason guard in `moe_god._validate_reasons`: every `W-L` / `W-L-T` record
  and every "N games" count cited in a reason must appear somewhere in the
  request text; otherwise the response fails validation (audit row). Reuse
  the record regex style from `moe._complete_unique_record_paths`.
- Tests: input-file round trip, hash mismatch refused, reason guard
  positive/negative.

### WP3 — Disagreement report (0.5 d)

- `scripts/moe_grade.py` prints, per graded game with both arms: Brier for
  each arm, whether the legs agreed, and who was right where they differed;
  season totals: paired Brier difference with its standard error, agreement
  rate on legs, disagreement record. Add the mean-of-arms row to the ledger
  output (ledger only; it is not an expert).
- Tests with synthetic rules/judge rows on the same events.

### WP4 — Historical lines pull (0.5 d)

- `scripts/fetch_nfl_lines_history.py`: download the nflverse file, filter
  regular season 2016–2025, map teams to full names, write
  `data/nfl_lines_history.csv` (committed; small). Then fetch ESPN core odds
  for every 2024 and 2025 regular-season event (544 requests, paced, one
  provider), write `data/nfl_open_close.json`. Idempotent; re-run appends
  the current season when needed.
- Tests: parser fixtures for both formats; 32-team map asserted; sign
  convention test on a handful of known games.

### WP5 — Evidence overlap and per-market relevance (2 d)

- Record-tuple extractor across schemas: from each voice's
  `supporting_factors` / `counterarguments` text, collect `(W-L[-T], games)`
  tuples and cohort labels. Pairwise overlap = Jaccard over tuples.
- Weight adjustment in `build_feature_block`: voices ordered by expert id;
  a voice's weight is divided by `1 + overlap with all higher-ranked voices`
  (two voices reciting one table sum to about one voice). Record the overlap
  matrix in the feature block; the judge request carries it as a feature.
- Registry: each non-aggregator expert gets `markets: [side, total]` (or a
  subset). Proposed: schedule side+total, divisional side, win_total side,
  ak side+total, rating side+total. The side pool uses side-informed voices,
  the total pool total-informed ones; the judge request marks each voice's
  markets.
- Tests: synthetic duplicate voices; relevance masks; the Week 1 replay
  numbers on the persisted inputs (Seahawks pool margin from +4.2 toward
  +3.9; Rams side edge from 4.4% to roughly 3.2%).

### WP6 — Empirical margins (1.5 d, needs WP4)

- From `data/nfl_lines_history.csv` (2016–2025): for each closing-spread bin
  (one point wide, home-relative), the distribution of actual home margin;
  same for totals against the closing total. Persist as
  `moe/priors/nfl_margins_v1.json` with the seasons and game counts inside.
- `aggregator_policy.margin_model: normal | empirical`; `cover_probability`
  and `over_probability` consult the table when `empirical`, falling back
  to the normal outside the table's support. The table's version and hash
  ride in the input like the WNBA prior does.
- Calibration check on 2025 (held out from the table's fit if the table is
  built from 2016–2024 first, then rebuilt on all ten seasons for
  production) before the switch is flipped.

### WP7 — Rating voice (2 d, needs WP4)

- `moe_rating.py`: Elo with home advantage and margin-of-victory update,
  parameters `K`, `hfa`, `mov_scale`, regression to the mean between
  seasons. Warm up on 1999–2022 from the nflverse file, fit K/hfa on
  2023–2024, check 2025 (Brier vs the closing moneyline's fair probability;
  target within 0.01).
- Registered as expert `rating_elo`, `mode: model`, deterministic backend,
  `input_profile: rating`; emits winner, scores (from rating gap and league
  scoring rate), probability, margin, stars from the rating gap. Weekly
  update from finals via the existing ESPN fetchers.
- Approval: pending decision (auto-approve deterministic voices, or keep
  the human gate). Default: human gate.

### WP8 — Backtest harness (2.5 d, needs WP6 and WP7)

- `scripts/backtest_god.py`: replay 2023–2024 game by game — market from
  nflverse closing lines with juice, committee = rating voice (LLM voices
  have no history), rules arm over a grid: σ / margin model, λ, edge
  threshold, EV floor, star edges. Score Brier, CLV where ESPN open/close
  exist, flat units. Choose on Brier with units as a sanity check; confirm
  on 2025 untouched. Veto sizes calibrated separately on ESPN open→close
  for 2024–2025.
- Output: fitted values written into `aggregator_policy` (dated in the
  intake plan) and a result table in the intake plan.

### WP9 — Judge ensemble (1 d, after the runbook is routine)

- Three seeded runs per game; each persisted as an audit row against the
  same input hash; the judge's single row is the mean of the three
  probabilities. Waits for automation of judge runs or a routine runbook.

### WP10 — Refit on the ledger (data, ~50 graded games)

- Refit λ and veto sizes on live rows; compare with the backtest's values.

## Dependencies and parallelism

- Phase 1 = WP1, WP2, WP3, WP4: independent. Run as four worktrees
  (`git worktree add ../telegram-forwarder-<slug> -b god/<slug>`), each with
  its own VPS scratch clone. Merge order into main: WP4, WP1, WP3, WP2
  (WP2 touches `moe.py` and the CLI; WP1 touches `moe_god.py`; low overlap).
- WP5 touches `moe_god.py` heavily: start it after WP1 merges, not in
  parallel with it.
- WP6 and WP7 need WP4's files; WP8 needs WP6 and WP7. WP6 and WP7 can run
  in parallel with each other.
- After each merge: full suite on a fresh scratch clone, then deploy at a
  week boundary (Tuesday after the Monday game is graded is the natural
  slot). Phase 1 targets the Week 2 boundary (SAT SEP 12).

## Acceptance for the phase-1 deploy

- All suites green on the VPS scratch clone.
- Week 1 replay test reproduces the roadmap table: Seahawks side and Over
  pass under veto/floor; 49ers +3.5 still bets.
- `--input-file` round trip persists the exact shown request; hash mismatch
  refused; reason guard rejects an invented record.
- `moe_grade.py` prints the paired report; first finals Wednesday night,
  grade Thursday morning.
- Skill runbook updated; docs and this status log updated.

## Decisions and defaults

Read the Desk artifact's `decisions` collection at the start of every
implementation session (Artifact `read_db`, collection `decisions`). Defaults
used when a decision is unanswered:

- Veto sizes 0.5 / 1.0 points and 10 cents; EV floor 2% per unit.
- Rating voice keeps the human approval gate.
- Judge ensemble from Week 5 at the earliest.
- Voice selection rule stays "registry default model, one row per expert".
- Week 1 rows stay pending until a human approves them.

## Status log

- 2026-09-07 — plan written from the roadmap page; nothing started. Week 1
  God Expert rows (two rules, two judge) persisted pending on 2026-09-06.
