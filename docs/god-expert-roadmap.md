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
  # scripts/godbuild_test.sh <slug> <worktree-dir> [extra modules...] does all of this:
  ssh root@209.38.51.86 'su - forwarder -c "git clone -q /home/forwarder/app /tmp/godbuild-<slug>"'
  # tar the files that differ from origin/main over the clone, strip their CRs, chown forwarder, then:
  su - forwarder -c "cd /tmp/godbuild-<slug> && ~/venv/bin/python -m unittest scripts.test_moe_god scripts.test_moe scripts.test_moe_ak scripts.test_moe_win_total scripts.test_generate_moe_opinion_cli scripts.test_intake_bot scripts.test_god_judge_runner scripts.test_nfl_lines_history scripts.test_moe_margins scripts.test_moe_rating scripts.test_moe_backtest scripts.test_moe_grade"
  ```

  Each worktree uses its own scratch clone so parallel runs never collide.
  Two pitfalls the helper absorbs (2026-09-07): a space-separated module
  list must be `printf %q`-quoted for ssh or the remote shell re-splits it
  (phase 2's first baseline ran two modules and reported them as the
  suite), and `git` inside the clone must run as forwarder (root trips the
  dubious-ownership check). The VPS venv has no numpy/scipy: fits are pure
  stdlib.
- Deploy: commit on main → push → `git pull` **as root** in
  `/home/forwarder/app` (root owns files there; a pull as `forwarder` fails
  half-way and leaves a partial checkout) → `systemctl restart
  telegram-intake.service` only when `moe.py` or `intake_bot.py` changed.
- Units: `deploy/systemd/` is the source of truth; install with `cp` into
  `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now
  <timer>`, and `bash scripts/check_deploy_sync.sh` must report all in sync.
  The headless judge call on the VPS is `claude -p --safe-mode`: plugins are
  off, so it leaves the Telegram channels session alone (verified
  2026-09-07), and `--bare` would be wrong because it never reads the OAuth
  token. The judge never runs from an interactive session.
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
  in places). Sign convention (verified 2026-09-07 by WP4 on the data):
  `spread_line` is positive when the home team is favored, so the repo's
  `home_spread = -spread_line`; the moneyline favorite agrees in 2,731 of
  2,746 games. `NFLVERSE_TEAMS` in `scripts/fetch_nfl_lines_history.py` is
  the asserted 32-team map; `data/nfl_lines_history.csv` is the pulled file.
- **ESPN core odds** —
  `https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{id}/competitions/{id}/odds`.
  Returns `items[]` per provider. The `ESPN BET` item carries `open`,
  `close`, and `current` blocks (total line and juice in the top-level
  blocks; spread line and moneyline inside `homeTeamOdds`/`awayTeamOdds` per
  block) for 2024 and 2025 through week 12; from 2025 week 13 the pregame
  provider is DraftKings (79 games), and 2024 wk2 PIT@DEN has no pregame
  provider at all. 2023 is unusable, not merely inconsistent: its blocks put
  a price in the line field and carry no `open`. Resolved 2026-09-07 by WP4:
  the per-team `pointSpread.american` is home-relative and is the spread of
  record (the top-level `spread` agrees in 544/544 events; `details` is
  favorite-relative, which was the apparent disagreement). ESPN's close is
  within half a point of nflverse's for 86% of spreads and 75% of totals;
  seven near-pick'em games favor different teams. Pulled into
  `data/nfl_open_close.json` (544 events, 464 with ESPN BET open+close).
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
- Built 2026-09-07. Direction pinned by the acceptance test: "adverse" means
  the market moved away from the bet's side (the bet's own line or price got
  cheaper), not that the bettor's number got worse. Every leg carries
  `pass_reason`; `movement_since_open()` and `price_cents()` are module-level
  so persisted inputs without the price deltas replay. Open: v1 step 7 no
  longer mentions the veto or the floor (bump to v2 or leave), and a knob of
  0 vetoes every leg rather than disabling the check.

### WP2 — Judge plumbing and automation (2 d)

- `--input-file <path>` on `scripts/generate_moe_opinion.py`, valid with
  `--agent-response` and `--deterministic`: the file's JSON becomes the
  persisted `input_json` (its hash must equal `--expected-input-sha256`
  when both are given); `generate_opinion` accepts a prebuilt payload. The
  judge stops racing the 30-minute lines fetcher; both arms can be pinned to
  one sheet state.
- **Judge automation (decided 2026-09-07): judge runs happen only from a
  fresh headless session, never from an interactive one.**
  `scripts/god_judge_runner.py`, run by `god-judge.timer` every 30 minutes
  at :12 and :42 (after the lines fetcher), does the following per upcoming
  game whose committee is complete (an approved row for every enabled
  non-aggregator expert):
  1. Build the aggregator input and the judge request into a fresh temp
     directory. Compute a **committee key** = SHA-256 of the sorted voice
     opinion ids plus the latest full-game lines and prices — not the
     capture timestamp, which changes every fetch. Skip the game when a
     valid judge row with the same committee key already exists, and stop
     entirely two hours before kickoff.
  2. Run the rules arm on the same input file (`--deterministic
     --input-file`) so both arms share one sheet state.
  3. Launch `claude -p` with `--model claude-fable-5-1 --effort max`, all
     tools disallowed, from an empty working directory whose environment
     holds no sheet credentials, passing exactly the registered prompt plus
     the request and asking for the JSON object only. Capture stdout.
  4. Persist with `--agent-response <resp> --input-file <req>
     --expected-input-sha256 <hash> --model claude-fable-5-1
     --generation-effort max`, backend `claude_headless` (new enum value,
     allowed for `god_judge` next to `agent_runtime`, so the row says how it
     was produced). Delete the temp directory.
  5. DM the reviewer through the watchdog bot that new pending rows exist
     for the game. The runner never approves anything.
  Units live in `deploy/systemd/god-judge.service` + `.timer` with a
  `run_god_judge.sh` runner (`set -o pipefail`, `TimeoutStartSec` above the
  worst case, its own healthcheck URL), per the infra rules in CLAUDE.md.
  Budget note: each trigger is one subscription call at max effort; measure
  weekly usage for two weeks before WP9 multiplies it.
- Manual fallback runbook in `.claude/skills/generate-nfl-moe-opinion/SKILL.md`:
  a fresh interactive session, the same inputs, the same persist command.
- Reason guard in `moe_god._validate_reasons`: every `W-L` / `W-L-T` record
  and every "N games" count cited in a reason must appear somewhere in the
  request text; otherwise the response fails validation (audit row). Reuse
  the record regex style from `moe._complete_unique_record_paths`.
- Tests: input-file round trip, hash mismatch refused, reason guard
  positive/negative, runner committee-key dedupe and kickoff cutoff with a
  stubbed `claude` invocation.
- Built 2026-09-07. `--input-file` holds the full aggregator input (the
  judge's normalization needs the labels and voice names); the masked
  request is derived from it and is what the judge row persists and what
  `--expected-input-sha256` checks. `--show-input --input-file` prints that
  request without re-reading the sheet, so the manual runbook pins both arms
  to one state. The runner defaults to `--safe-mode`, not `--bare`: the
  installed CLI's help (2.1.263) says `--bare` never reads OAuth, which
  would refuse the subscription token. The rules arm is deduped on the
  committee key too, so a judge retry never duplicates it; a rejected judge
  row does not block a fresh run. The reason guard also accepts numbers the
  request carries in structured form (projected scores, the winner-vote
  split, track-record tallies, count-like integer keys, and the cohort a
  cited record implies — the real Rams response needed `2-2` and `17-8 →
  25 games`). Usage per call is appended to `logs/god_judge_runs.jsonl`.

### WP3 — Disagreement report (0.5 d)

- `scripts/moe_grade.py` prints, per graded game with both arms: Brier for
  each arm, whether the legs agreed, and who was right where they differed;
  season totals: paired Brier difference with its standard error, agreement
  rate on legs, disagreement record. Add the mean-of-arms row to the ledger
  output (ledger only; it is not an expert).
- Tests with synthetic rules/judge rows on the same events.
- Built 2026-09-07. Pairs prefer the judge row whose request hash links to a
  rules row's input hash; the mean-of-arms row is graded with the rules
  row's persisted market and policy merged over `DEFAULT_POLICY`, so Week 1
  mean rows carry the veto and floor the arms never saw. `--json` is now
  `{"scoreboard", "disagreement", "mean_of_arms"}`.

### WP4 — Historical lines pull (0.5 d)

- `scripts/fetch_nfl_lines_history.py`: download the nflverse file, filter
  regular season 2016–2025, map teams to full names, write
  `data/nfl_lines_history.csv` (committed; small). Then fetch ESPN core odds
  for every 2024 and 2025 regular-season event (544 requests, paced, one
  provider), write `data/nfl_open_close.json`. Idempotent; re-run appends
  the current season when needed.
- Tests: parser fixtures for both formats; 32-team map asserted; sign
  convention test on a handful of known games.
- Built 2026-09-07: 2,639 games in the CSV (324 KB), 544 events in the JSON
  (497 KB; ESPN BET open+close for 464, DraftKings for 2025 weeks 13–18, one
  event without a pregame provider). Live-odds providers are never selected.
  Two closes now exist (nflverse in the CSV, ESPN in the JSON); WP6 must
  name which it fits — nflverse for depth, ESPN only for open→close movement
  is the suggestion. `.gitignore` is `data/*` with the two files re-included
  (and `angles/data/` re-ignored explicitly).

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
- Built 2026-09-07. Tuples are `[W, L, T, games]` from every `W-L`/`W-L-T`
  token in the FULL factor lists (the count nearest the record in the same
  item, else W+L+T), persisted per voice as `evidence`; overlap is Jaccard
  keyed by voice id; the discount divides the Hedge weight by one plus the
  summed overlap with the voices ranked before it (`hedge_weights` and the
  discounted `weights` both persist). Registry `markets`: schedule and ak
  side+total, divisional and win_total side only; an empty pool takes the
  market expectation. Legacy inputs replay unchanged and their judge
  requests stay byte-identical. Prompts bumped to `god_rules/v2.md` (pool
  step and the veto/floor in step 7) and `god_judge/v2.md`. Replay under
  the formula as specified: Seahawks pool margin +4.25 → +4.21 (divisional
  and schedule share 4 of 16 distinct tuples, overlap 0.25, schedule at
  0.8); Rams side edge 4.42% → 4.38% (overlap 0.11 and 0.10). The roadmap's
  +3.9 and 3.2% need the divisional/schedule pair to pool as about one
  voice, which a rank-ordered Jaccard discount does not produce; and the
  "two voices reciting one table sum to about one voice" phrase is false
  under the rank rule (identical voices pool as 1.0 and 0.5). Open for the
  user: keep the rank rule, or divide every voice by one plus its overlap
  with all other voices (n copies of one table then sum to exactly one).

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

- Built 2026-09-07. Close of record: nflverse (the CSV, 2016–2025), never
  ESPN. Residuals against the market (margin + home_spread, total − line) in
  floor-based one-point bins ([k, k+1)), `min_games` 30, half-push
  convention P(r > t) + ½P(r = t), linear interpolation between half-point
  lattice points, normal fallback off support. `margin_model` rides in the
  policy (so in every input hash) and the table's sha256 rides in the input
  under `margin_table`; the judge request repeats both; legacy inputs derive
  byte-identical requests. 2025 hold-out (fit 2016–2024): a wash on spreads
  (Brier 0.21611 vs 0.21606, log loss a hair better, better at −7/−3, worse
  at +3), slightly worse on totals (0.21947 vs 0.21894) — the switch stays
  `normal`. Standard library only (no numpy/scipy on the VPS). Table build:
  `python scripts/build_nfl_margins.py` (idempotent; `--dry-run` prints the
  check). Tests: `scripts.test_moe_margins`.

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
- Approval (decided 2026-09-07): the human gate stays for every row,
  including the rating voice's. Add a bulk review mode to
  `scripts/review_moe_opinion.py` (`--expert rating_elo --week N`, 0.25 d)
  that prints the week's rating rows as one table and approves them in one
  command after a human has looked at it.
- Built 2026-09-07. `moe_rating.py` + registry `rating_elo` (mode `model`,
  profile `rating`, schema 9, `default_model: deterministic`,
  `markets: [side]` — side only, decided at the merge: its total is the
  league scoring rate and must not dilute the total pool; the WP5 proposal
  said side+total — enabled) + spec `moe/prompts/rating_elo/v1.md`;
  prior `moe/priors/nfl_elo_v1.json` from `scripts/fit_nfl_elo.py` (K 19,
  hfa 32, regression 1/3, points_per_elo 21.99 by least squares; fit Brier
  0.2224 on 2023–24). 2025 check: Elo Brier 0.2224 vs closing moneyline
  0.2116, +0.0108 — the within-0.01 target is missed by 0.0008 and the prior
  says `target_met: false`; a fit-season probe of the damping constant and
  the probability scale could not separate the variants (<0.0003), so the
  plain 538 form stays. The wider CSV IS committed: `data/nfl_lines_history.csv`
  now spans 1999–2025 (6,967 games; the 2016–2025 rows are byte-identical)
  and `DEFAULT_SEASONS` is `1999-2025`. Bulk review is
  `scripts/review_moe_opinion.py --expert rating_elo --week N --reviewed-by
  <you> [--approve]` after `scripts/generate_rating_week.py --season S --week
  N`; nothing approves on its own. Deploy note: with `rating_elo` enabled the
  judge runner's committee requires an approved rating row per game, so the
  week's rating rows must be generated and approved before the timer judges
  it (or ship with `enabled: false` until then).
- Review changed 2026-09-07 (night): rows are approved on validation at
  generation (registry `review: validation`; see Decisions). The weekly
  command is now the whole procedure; the bulk review stays for legacy
  pending rows. Tests: `test_review_policy_is_deterministic_only`,
  `test_generate_week_approves_earlier_pending_rows`, and the end-to-end
  generation test in `scripts/test_moe_rating.py`.

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
- Built 2026-09-07. `moe_backtest.py` (library, stdlib, no `moe` import,
  so it runs on Windows) + `scripts/backtest_god.py` (`grid`, `veto`,
  `clv`, `ledger`; global `--json`). Every number comes from the production
  arithmetic, never a copy: `market_block_from_lines` (the body of
  `build_market_block`, factored out; a missing opening never vetoes) →
  `rating_estimate` (factored out of `build_rating_input`) →
  `voice_from_row` → `build_feature_block` → `rules_arm_response` →
  `apply_policy` → `_leg_result`. Committee = the rating voice alone (Elo
  with the committed prior's parameters, replayed from 1999 so every
  pregame rating is leak-free; the previous season's scoring rate as its
  total; side pool only per the registry, so the total pool is empty and
  no total ever fires under `normal` — `sigma_total` and the total veto are
  not identifiable from this committee and keep their values). Market =
  the nflverse close with juice (no opening, so the veto is inert in
  `grid`); the empirical model reads an as-of table built from 2016 to the
  season before the one scored (`moe_god.margin_table_override`, backtests
  only), never the committed file. Grid: λ {0, .25, .5, .75, 1} ×
  σ_margin {12, 13, 13.5, 14, 15} × margin model × edge {.02 … .05} × floor
  {0 … .03} × two star ladders = 1,600 policies over 544 games in 9 s.
  Result (fit 2023–2024, confirm 2025 untouched): ML Brier is monotone in
  λ — 0 → 0.2094 (= the market), 0.25 → 0.2109, 0.5 → 0.2135, 0.75 →
  0.2174, 1 → 0.2224 (= Elo) — so the printed rule picks λ = 0 with every
  other knob at its registry value; on 2025 λ = 0 scores 0.2122 (market
  0.2121) against the registry's 0.2156, and the registry policy on this
  committee bets 274 legs 133-134-7 for −10.5u (−3.8%) in the fit window
  and 122 legs 58-63-1 for −9.0u (−7.4%) in 2025. CLV (bets at the ESPN
  open, 2024–2025, 543 games): registry 235 legs, −30.0u (−12.8%), mean
  CLV +0.07 points. **Reading: nothing was written to `aggregator_policy`.**
  λ = 0 is a statement about a rating-only committee — the Elo voice adds
  nothing beyond the closing line, consistent with WP7's 0.2224 vs 0.2116
  — not about the live five-voice committee, which the backtest never saw;
  the policy stays until the ledger refit (WP10) has ~50 games of the real
  committee. Veto calibration (ESPN open → close, 543 events): the side a
  spread moved away from goes 206-186 (−0.7%) at ≥ 0.5, 172-164 (−3.2%) at
  ≥ 1, 88-88 (−5.5%) at ≥ 1.5, 69-73 (−7.9%) at ≥ 2 — the harness's rule
  proposes 2.0, but the gap to 0.5 sits inside one standard error at 142
  legs, so the decided 0.5 stays; the total veto has the wrong sign (the
  side a total moved away from wins +7.7% at ≥ 1.5 on 189 legs) and stays
  at 1.0 as decided; the price veto is the one clear signal (≥ 10 cents:
  394 legs, −12.0%; ≥ 5: 831 legs, −8.9%) and stays at 10. Full tables in
  the intake plan. Test: `scripts/test_moe_backtest.py` (21 tests: both
  refactors pinned against the old arithmetic, hand-built scoring cases,
  the as-of table, the veto table, the ledger mode on the Week 1 fixtures,
  the raw Elo 2025 Brier reproducing the prior's 0.2224).

### WP9 — Judge ensemble (1 d, after two weeks of runner usage)

- The runner launches three headless calls per trigger instead of one; the
  three responses persist as audit rows with a `sample` generation status
  that the review and display paths ignore, and the judge's single row per
  trigger carries the mean of the three probabilities (reasons from the
  sample closest to the mean). One row per game reaches the human gate, so
  the ensemble does not multiply approvals. Starts once two weeks of
  single-sample runner usage show the subscription carries three times the
  calls. Usage is read from `logs/god_judge_runs.jsonl` (one line per call:
  duration, cost, turns); remember the current committee key already
  re-judges a game on every price move.
- Built 2026-09-07, default off. `scripts/god_judge_runner.py --samples N`
  (`GOD_JUDGE_SAMPLES`, 1–5, default 1). N = 1 is byte-identical to the
  single-sample path. N ≥ 2: N sequential calls per game (each runs-log
  line carries `sample`/`samples`), every response persists through
  `generate_opinion(sample=True)` as an audit row — `generation_status`
  `sample`, `review_status` `not_applicable`, whether or not it validated
  (a failed sample keeps that status with its `generation_error`) — and the
  one judge row of the trigger is `moe_god.ensemble_response`: the means of
  the valid samples' three numbers through `coherent_estimate` (the fence
  and sign step factored out of `rules_arm_response`, byte-identical on the
  Week 1 fixtures), the reasons of the sample closest to the mean, and an
  `ensemble` block (`size`, `valid`, `samples`, `estimates`,
  `reasons_from`, `rule`) validated by `normalize_aggregator_opinion` (judge
  only; the rules arm rejects it) and copied into
  `calibration_summary_json`. No valid sample → the first response persists
  as an ordinary invalid judge row, so the two-invalid-rows stall is
  unchanged; sample rows are neither valid nor invalid and never count.
  `approved_opinions`, the bot's display, `week_rows`, and `store.review`
  already ignore anything not `valid` (pinned). `deploy/systemd/god-judge.service`
  `TimeoutStartSec` 3600 → 9000 (3 games × 3 samples × 900 s; `cp` +
  `daemon-reload` at deploy). Start the ensemble with `GOD_JUDGE_SAMPLES=3`
  in `.env` + `syncenv` at a week boundary once two weeks of single-sample
  usage are read from the runs log (3 calls so far: 76–108 s API time,
  $0.35–0.63, 7–9.5k output tokens each). The manual fallback stays
  single-sample. Tests: `EnsembleTests` and `SampleRowTests` in
  `scripts/test_moe_god.py`, `EnsembleRunnerTests` in
  `scripts/test_god_judge_runner.py` (a stub that varies per call).

### WP10 — Refit on the ledger (data, ~50 graded games)

- Refit λ and veto sizes on live rows; compare with the backtest's values.
- Tooling built 2026-09-07; the refit itself waits for data.
  `scripts/backtest_god.py ledger` replays persisted `god_rules` rows
  (valid and approved, or `--include-pending`) under the same grid: each
  row's persisted voices re-pooled on the row's own market (opening and
  latest are both persisted, so the veto can fire here), graded against
  ESPN finals and the last snapshot before kickoff, plus a per-knob veto
  sweep that attributes every vetoed leg to one threshold, and a comparison
  against the grid's fitted values (`--grid-json`). Reads the sheet on the
  VPS or `--rows-json/--finals-json/--snapshots-json` offline; nothing is
  persisted or approved. Under `MIN_REFIT_GAMES` (50) graded games the
  report says so and is informational. On the two Week 1 fixtures with
  synthetic finals: the rows re-pool under WP5 (Seahawks 0.6258 vs the
  persisted 0.6259), Seahawks −3.5 is vetoed on the price move, Over 44.5
  now clears the floor and wins, 49ers +3.5 wins, the Rams total stays
  under the bar → 2-0-0. Run it on a Tuesday once `moe_grade.py` has ~50
  graded games and compare with WP8's table.

## Dependencies and parallelism

- Phase 1 (WP1–WP4) landed and was deployed on 2026-09-07 (status log).
  Built as four worktrees (`git worktree add ../telegram-forwarder-<slug>
  -b god/<slug>`), each with its own VPS scratch clone, merged in the order
  WP4, WP1, WP3, WP2; the one merge interaction was WP3's tests meeting
  WP1's veto on the default test market.
- Phase 2 (WP5–WP7) landed on main on 2026-09-07 (status log), not yet
  deployed. Three worktrees (`god/overlap`, `god/margins`, `god/rating`),
  one subagent each in parallel, with ownership rules instead of a rebase:
  WP5 owned every substantive `moe_god.py` change and the prompt bumps, WP6
  kept to the margin model, WP7 added one lens entry. Merged WP5, WP6, WP7.
  Two merge interactions: WP6's table lookup meeting WP5's per-market pools
  in `build_feature_block` and `build_judge_request` (both kept, resolved by
  hand), and WP5's registry test meeting WP7's `rating_elo` (one
  expectation). The wider CSV is committed (1999–2025).
- WP8 needs WP6 and WP7. Its open→close calibration has 543 usable events
  (392 with movement): ESPN BET through 2025 week 12, DraftKings after.
- Phase 3 (WP8–WP10) landed on main on 2026-09-07 (status log), not yet
  deployed. Three worktrees: `god/backtest` (WP8 + WP10's tooling) and
  `god/ensemble` (WP9) built by forked agents in parallel with disjoint
  regions of `moe_god.py` (the backtest owned `build_market_block` and
  `margin_table_for`, the ensemble `rules_arm_response`'s coherence step
  and `normalize_aggregator_opinion`), and `god/guard` (the reason-guard
  fix for the evening's two live rejections); merged on an integration
  branch in that order with no conflicts, then fast-forwarded onto main.
- After each merge: full suite on a fresh scratch clone — twelve modules
  now: `scripts.test_moe_god scripts.test_moe scripts.test_moe_ak
  scripts.test_moe_win_total scripts.test_generate_moe_opinion_cli
  scripts.test_intake_bot scripts.test_god_judge_runner
  scripts.test_nfl_lines_history scripts.test_moe_margins
  scripts.test_moe_rating scripts.test_moe_backtest scripts.test_moe_grade`
  (`bash scripts/godbuild_test.sh <slug> <dir>` runs them) — then deploy at a week boundary (Tuesday after the Monday
  game is graded is the natural slot).

## Acceptance for the phase-1 deploy

Status 2026-09-07: every item below is met on main (238 tests on a fresh
scratch clone; the Week 1 replay numbers match the table; `--input-file`
round trip and reason guard are pinned by tests; `moe_grade.py` prints the
paired report; the skill runbook is updated). Deploy pending.

- All suites green on the VPS scratch clone.
- Week 1 replay test reproduces the roadmap table: Seahawks side and Over
  pass under veto/floor; 49ers +3.5 still bets.
- `--input-file` round trip persists the exact shown request; hash mismatch
  refused; reason guard rejects an invented record.
- `moe_grade.py` prints the paired report; first finals Wednesday night,
  grade Thursday morning.
- Skill runbook updated; docs and this status log updated.

## Acceptance for the phase-2 deploy

Status 2026-09-07: every item below is met on main (296 tests on a fresh
scratch clone; merge commits 9960ae1, b7834eb, db518b4). Not pushed; deploy
pending, the user's call at a week boundary. **Deployed 2026-09-07 at
21:06 EDT by another session** together with the daily `moe-grade.timer`
(main `7a89d41`; pushed, pulled as root, `telegram-intake` restarted, the
timer armed for 05:23 ET). That session generated the 17 Week 1
`rating_elo` rows, all pending: until they are approved the judge runner
skips every game as "committee incomplete, no approved row for rating_elo".
Since the validation review (phase 3) the first
`generate_rating_week.py --season 2026 --week 1` after the phase-3 deploy
approves them.

- All ten suites green on the VPS scratch clone.
- Week 1 replay pinned under the overlap formula as specified (Seahawks pool
  margin +4.25 → +4.21, Rams side edge 4.42% → 4.38%); the gap to the
  roadmap's +3.9 and 3.2% is documented next to WP5, not papered over.
- The 2025 hold-out calibration is read: the table is a wash, so
  `margin_model` stays `normal`.
- The Elo check is pinned (2025 Brier 0.2224 vs closing moneyline 0.2116,
  `target_met: false` in the prior); the miss is 0.0008 past the target.
- Bulk review prints the week before it approves; nothing approves on its
  own; the judge never ran in this phase.
- CLAUDE.md, the skill runbook, the intake plan, and this status log updated.

Deploy runbook (when the user says go): no env change, so no `syncenv`;
`git push`; on the VPS `git pull` as root in `/home/forwarder/app`;
`systemctl restart telegram-intake.service` (`moe.py` changed); no systemd
unit changed, so `bash scripts/check_deploy_sync.sh` should still report in
sync. **Then, before the next `god-judge.timer` pass**: as forwarder in
`~/app`, `python scripts/generate_rating_week.py --season 2026 --week <N>`,
read `python scripts/review_moe_opinion.py --expert rating_elo --week <N>
--season 2026 --reviewed-by <you>`, and re-run it with `--approve` —
otherwise every game skips as "committee incomplete, no approved row for
rating_elo" (a print in the journal, no DM). Alternative: set
`rating_elo.enabled: false` in `moe/experts.yaml` before pushing and flip
it at a later week boundary.

## Acceptance for the phase-3 deploy

Status 2026-09-07: every item below is met on main (340 tests on a fresh
scratch clone, twelve modules, after merging the daily grading timer that
another session landed on main meanwhile). Not pushed; deploy pending, the
user's call. Phase 2 is already live (see above), so phase 3 deploys on top
of it.

- All twelve suites green on the VPS scratch clone.
- The backtest's selection is printed with its rule and confirmed on 2025
  untouched; the reading (a rating-only committee; λ = 0 not written) is
  recorded next to WP8 and `aggregator_policy` is unchanged.
- The ensemble is wired and pinned by tests with `GOD_JUDGE_SAMPLES`
  defaulting to 1; single-sample rows are byte-identical to before.
- The ledger refit tool runs on the Week 1 fixtures and reports itself
  informational below 50 games.
- The reason guard accepts decimal fragments and home-first scores (the two
  live rejections of 2026-09-07); the Week 1 evidence sets are unchanged.
- CLAUDE.md, the skill runbook, the intake plan, and this status log updated.

Deploy runbook (phase 2 + phase 3 together, when the user says go):

1. Local: no env change, so no `syncenv`; `git push`.
2. On the VPS as root in `/home/forwarder/app`: the server tree carries
   uncommitted work from a VPS session (the nightly ungraded audit: edits
   to `CLAUDE.md`, `deploy/systemd/README.md`, two hooks, a mode change on
   `deploy/claude_auth_watchdog.py`, plus untracked
   `scripts/ungraded_audit.py`, `run_ungraded_audit.sh`,
   `docs/ungraded-audit.md`, two units and a test). Commit it there first,
   or `git stash push -m "server wip"` → `git pull` → `git stash pop` and
   resolve `CLAUDE.md`/README by keeping both sides; the untracked files
   do not block the pull.
3. `cp deploy/systemd/god-judge.service /etc/systemd/system/ && systemctl
   daemon-reload` (TimeoutStartSec 9000; the oneshot needs no restart),
   then `bash scripts/check_deploy_sync.sh` → all in sync.
4. `systemctl restart telegram-intake.service` (`moe.py` changed).
5. Already live since the phase-2 deploy: the runner needs an approved
   `rating_elo` row per game. The 17 Week 1 rows are pending; after this
   deploy, `python scripts/generate_rating_week.py --season 2026 --week 1`
   (as forwarder in `~/app`) approves them on validation and adds any
   missing ones — no review command. The judge skips every game until it
   runs.
6. Leave `GOD_JUDGE_SAMPLES` unset (= 1) until the two-week usage read.
7. The Seahawks committee key stays stalled on its two invalid rows until
   the committee changes (a BetOnline move or a new approved voice row);
   the guard fix takes effect on the next fresh key. If the Wednesday game
   needs a judge row before that, the skill's manual fallback on a fresh
   session is the path.

## Decisions

Decided 2026-09-07 in chat, recorded here and on the Desk page:

- Veto 0.5 points on spreads, 1.0 on totals, 10 cents on price; EV floor
  2% per unit. The backtest (WP8) may refine them.
- The human gate stays for every row, including the rating voice's (WP7).
  **Superseded 2026-09-07 (night), user decision, for the rating voice
  only:** its rows are approved on validation at generation (registry
  `review: validation`; `moe_god.review_policy`; `reviewed_by=validation`,
  hash-bound like a human approval, because `normalize_rating_opinion`
  already requires every number to equal the input's own estimate), and
  `generate_rating_week.py` approves the week's earlier valid pending rows
  the same way. The gate stays for every LLM row and for both God Expert
  arms: the registry loader refuses `validation` on any non-`mode: model`
  expert.
- Judge runs are automated from a fresh headless session (WP2); the
  ensemble (WP9) follows once runner usage is measured.
- Grading cadence (decided 2026-09-07 in chat): `moe-grade.timer` runs
  `scripts/moe_grade.py --write --notify` daily at 05:23 ET. Grading is
  deterministic and the ledger dedupes on opinion id, so the daily pass
  is the whole latency budget; the operator DM arrives only when rows
  were appended (intake plan, "Daily MOE grading timer").

Still open on the Desk page — read its `decisions` collection at the start
of every implementation session (Artifact `read_db`, collection
`decisions`): the four Week 1 approvals, the two Week 1 bets, the Rams
committee refresh, the voice selection rule (default stays "registry default
model, one row per expert"), stake language, the server's dirty tree, and
the two hidden rows (grading cadence is decided above). Week 1 rows stay pending until a human
approves them.

Open after phase 1 (2026-09-07) — the user's call, nothing in code assumes
an answer:

- ~~`moe/prompts/god_rules/v1.md` step 7 omits the veto and the floor.~~
  Closed 2026-09-07 by WP5: `god_rules/v2.md` (needed anyway for the pool
  changes) states the veto and the floor in step 7 and, after the merge,
  the `margin_model` switch in step 3. Rows persisted from the next deploy
  hash v2.
- A veto knob of 0 vetoes every leg that has opening data. If 0 should mean
  disabled, the checks need `knob > 0`.
- The reason guard grounds a cited "N games" through the cohort a cited
  record implies (`17-8` → 25 games); the real Week 1 Rams response needs
  it. Keep, or drop and accept that row as an audit record.
  Phase 3 (2026-09-07): the first two live headless responses were
  rejected for `0-20` (inside "implied totals 24.0-20.5") and `27-21` (a
  voice's 21-27 projection written home-first) — guard false positives,
  fixed: a digit-dot before or a dot-digit after a record is a decimal
  fragment, and both score orders are derived. The cohort question above
  stays open.
- Judge call volume: the committee key includes the latest prices, so every
  BetOnline price move re-judges the game (cap 3 calls per pass, 144 a
  day). Read `logs/god_judge_runs.jsonl` for a week, or coarsen
  `committee_key` to lines only.
- `GOD_JUDGE_HEALTHCHECK_URL` is unset; `ping_hc` no-ops until it is added
  to the local `.env` and synced.
- ~~WP6 must name its close.~~ Decided 2026-09-07: nflverse (the CSV,
  2016–2025); the ESPN JSON is reserved for open→close movement (WP8).
- Rejected judge rows re-run on the same committee key (session decision
  2026-09-07 in `scripts/god_judge_runner.py`); pending and approved rows
  block. Reverse it if a rejection should stay final.

Open after phase 2 (2026-09-07) — the user's call, nothing in code assumes
an answer:

- Overlap weight rule. As specified (rank-ordered: a voice divides its
  Hedge weight by one plus its overlap with the voices ranked before it),
  identical voices pool as 1.0 and 0.5, and the Week 1 pair moves the pool
  only slightly. The alternative — divide every voice by one plus its
  overlap with all other voices, so n copies of one table sum to exactly
  one — is a one-line change in `overlap_adjusted_weights`; neither reaches
  the roadmap's +3.9/3.2% on these texts (Jaccard 0.25 and 0.11).
- Elo target: the 2025 Brier is 0.0008 past "within 0.01 of the closing
  moneyline". Accept (the fit data cannot separate the knobs; recommended),
  or widen the fit window (2016–2024) — a plan change.
- `margin_model` stays `normal`; `min_games` 50 scores marginally better on
  spreads at 85% coverage (a knob in `scripts/build_nfl_margins.py`).
- `rating_elo` is `enabled: true`: the judge needs an approved rating row
  per game (deploy runbook above), or ship `enabled: false` first.
- `god_judge/v2.md` has not judged a live game yet; the first timer pass
  after the deploy is the check that the new request fields (overlap,
  markets, hedge weights by label) do not confuse it.
- The extractor reads a scoreline such as "won 23-20" as a record (pinned as
  a known limitation); harmless unless two voices cite the same scoreline.

Open after phase 3 (2026-09-07) — the user's call, nothing in code assumes
an answer:

- Spread veto size. The open→close table (543 events) says the side a
  spread moved away from loses more the bigger the move (−0.7% at ≥ 0.5,
  −7.9% at ≥ 2.0) and the harness's rule proposes 2.0; the difference is
  within one standard error at 142 legs and the 0.5 was decided in chat,
  so it stays. Revisit with the ledger refit.
- Total veto. The same table has the wrong sign (the side a total moved
  away from wins +7.7% at ≥ 1.5 on 189 legs). Keep 1.0 as decided, or
  disable it — a knob of 0 vetoes every leg, so disabling needs the
  `knob > 0` change from the phase-1 list.
- `shrink_lambda`. The rating-only backtest prefers 0 (the market alone);
  the live committee has four model voices the backtest cannot score.
  Keep 0.5 until WP10 has ~50 graded games.
- Ensemble start. Read `logs/god_judge_runs.jsonl` after two weeks of
  single-sample runs (lines now carry `sample`/`samples`); each trigger
  then costs three calls (~100 s and about $0.5 each today).
- Stalled Seahawks committee (two invalid judge rows on key `1c6f9789…`).
  Only a committee change or a manual fallback run produces a judge row.
- `calibration_summary_json` now always carries an `ensemble` key (null
  outside the ensemble); persisted rows are untouched.

## Status log

- 2026-09-07 — plan written from the roadmap page; nothing started. Week 1
  God Expert rows (two rules, two judge) persisted pending on 2026-09-06.
- 2026-09-07 — three decisions recorded (veto sizes and floor, human gate
  for every row, headless judge automation); WP2 expanded with the runner
  and timer, WP7 with the bulk review mode, WP9 with the hidden-sample
  design. Phase 1 is now about four build days.
- 2026-09-07 — phase 1 built: WP1, WP2, WP3, WP4 implemented in four
  worktrees (`god/veto-floor`, `god/judge-runner`, `god/disagreement`,
  `god/lines-history`), merged into main in the order WP4, WP1, WP3, WP2, and
  verified on a fresh VPS scratch clone: `Ran 238 tests … OK` across
  `scripts.test_moe_god`, `test_moe`, `test_moe_ak`, `test_moe_win_total`,
  `test_generate_moe_opinion_cli`, `test_intake_bot`,
  `test_god_judge_runner`, `test_nfl_lines_history`. The four persisted
  Week 1 rows are fixtures under `scripts/fixtures/god_week1/`. Week 1 replay
  under the new policy: Seahawks −3.5 vetoed (`adverse move`, price −110 →
  +100), Over 44.5 `ev floor` (EV 0.0194), 49ers +3.5 still bets (EV 0.039),
  Rams total plain pass. Deviations recorded next to each WP below.
- 2026-09-07 19:13 EDT — phase 1 deployed: pushed 47b7c8b..cefd0a4, pulled
  as root, `telegram-intake` restarted, `god-judge.service`/`.timer`
  installed and enabled (first pass 19:42 EDT), `check_deploy_sync.sh` all
  in sync. The `--safe-mode` OAuth probe returned `ok` in one turn. The dry
  run showed the first pass will re-run both Week 1 games (their existing
  rows predate the committee key), so fresh headless judge rows and
  veto-aware rules rows arrive for the Seahawks and Rams games; the
  2026-09-06 rows stay pending until a human decides. Still unset:
  `GOD_JUDGE_HEALTHCHECK_URL` (`ping_hc` no-ops without it).
- 2026-09-07 (late evening) — phase 2 built: WP5, WP6, WP7 in three
  worktrees (`god/overlap`, `god/margins`, `god/rating`), one subagent each
  in parallel from a fresh session, merged into main in the order WP5, WP6,
  WP7 (merge commits 9960ae1, b7834eb, db518b4) and verified on a fresh VPS
  scratch clone: `Ran 296 tests … OK` across the eight phase-1 modules plus
  `scripts.test_moe_margins` and `scripts.test_moe_rating` (238 → 252 → 269
  → 296 along the way). Numbers: Seahawks pool margin +4.25 → +4.21 and Rams
  side edge 4.42% → 4.38% under the rank-ordered Jaccard discount (the
  roadmap's +3.9/3.2% are not reachable by that formula — recorded under
  WP5); the 2025 hold-out calibration is a wash (spread Brier empirical
  0.21611 vs normal 0.21606, total 0.21947 vs 0.21894; log loss a hair
  better on spreads, worse on totals; 89.7%/97.8% of 2025 games in
  supported bins), so `margin_model` stays `normal`; Elo (K 19, hfa 32,
  regression ⅓) 2025 Brier 0.2224 vs closing moneyline 0.2116 (+0.0108,
  target missed by 0.0008). Merge decisions: `rating_elo` informs the side
  pool only; the orchestrator added the `margin_model` sentence to
  `god_rules/v2.md` step 3. `scripts/godbuild_test.sh` is the committed
  test helper. Not pushed, not deployed; the user decides the timing.
- 2026-09-07 (night) — phase 3 built: WP8 and WP9 in two worktrees
  (`god/backtest`, `god/ensemble`) by forked agents in parallel, WP10's
  tooling inside WP8's harness, and a reason-guard fix (`god/guard`) for
  the evening's two live headless rejections (the 19:42 and 20:12 EDT
  passes judged the Rams — valid row `da0f43da` — and rejected the
  Seahawks twice, `0-20` and `27-21`, stalling that committee key);
  merged on an integration branch in the order backtest, ensemble, guard
  with no conflicts and fast-forwarded onto main; verified on a fresh VPS
  scratch clone: `Ran 335 tests … OK` across the ten phase-2 modules plus
  `scripts.test_moe_backtest` (per branch 317, 312, 298 → 335). Backtest
  reading: the rating-only rules arm cannot beat the closing line at any
  λ > 0 (fit ML Brier 0.2094 at λ 0 = the market, 0.2135 at the registry's
  0.5; 2025 0.2121 vs 0.2156), so `aggregator_policy` is unchanged; the
  veto table supports the price veto (−12% at ≥ 10 cents), is within noise
  on the spread veto, and has the wrong sign on the total veto. Ensemble
  default off (`GOD_JUDGE_SAMPLES` unset). Not pushed, not deployed.
  Meanwhile another session pushed and deployed phase 2 with the daily
  `moe-grade.timer` at 21:06 EDT (main `7a89d41`, merged into this branch;
  338 tests with its `scripts.test_moe_grade`), so phase 3 deploys on
  top of it; the 17 Week 1 rating rows it generated are pending approval
  and the judge skips every game until they are approved.
- 2026-09-07 (night, later) — user decision: rating rows are approved on
  validation, not by a person. Registry `review: validation` on
  `rating_elo` (`moe_god.review_policy`, refused outside `mode: model`);
  `generate_opinion` approves such a row at generation, hash-bound
  (`reviewed_by=validation`); `generate_rating_week.py` also approves the
  week's earlier valid pending rows and reports it; the bulk review prints
  a hint. Verified on a fresh VPS scratch clone: `Ran 340 tests … OK`. On
  main, not pushed; the first weekly run after the phase-3 deploy approves
  the 17 pending Week 1 rows. The guard fix must be live before that: the
  approvals change both Week 1 committee keys and re-judge both games.

## Session opener (phase 2)

Paste this as the first message of a fresh session started in the repo
directory. Phase 1 used the same shape (git history has it); later phases
change the scope line and the merge order.

```
You are orchestrating phase 2 of the God Expert roadmap in this repo.

Read, in this order, before anything else: CLAUDE.md (section "NFL MOE +
God Expert"); docs/god-expert-roadmap.md — the executable plan, whose
ground rules are settled and not up for debate, including its status log,
the "Open after phase 1" list, and the notes recorded under WP1–WP4; the
four "Completed — 2026-09-07" sections in docs/telegram-intake-plan.md;
moe_god.py; scripts/test_moe_god.py; scripts/god_judge_runner.py; the
module docstring of scripts/fetch_nfl_lines_history.py; and
.claude/skills/generate-nfl-moe-opinion/SKILL.md. Then read the Desk page's
decisions with the Artifact tool (action read_db, url
https://claude.ai/code/artifact/ac0d6098-d35c-45d5-85c4-cede602d06ab,
collection "decisions") and tell me what is decided and what is still open
before you write any code. If any decision there is an approval, show me
the exact review commands you would run and wait for my go-ahead.

Scope for this session: WP5 (evidence overlap and per-market relevance),
WP6 (empirical margins), WP7 (rating voice with its bulk review mode).
Nothing from WP8 onward.

Method: one git worktree per work package
(git worktree add ../telegram-forwarder-<slug> -b god/<slug>), one subagent
per worktree, all three in parallel. Each subagent implements its WP
exactly as the plan specifies, writes tests in the repo's unittest style,
and verifies on the VPS from its OWN scratch clone at /tmp/godbuild-<slug>:
clone from /home/forwarder/app as the forwarder user, overlay the files
that differ from origin/main with tar, strip CRs, chown to forwarder, run
~/venv/bin/python -m unittest scripts.test_moe_god scripts.test_moe
scripts.test_moe_ak scripts.test_moe_win_total
scripts.test_generate_moe_opinion_cli scripts.test_intake_bot
scripts.test_god_judge_runner scripts.test_nfl_lines_history plus its own
new modules, remove the clone, and report the exact test output. No
subagent touches ~/app, the live sheet, or approves anything. WP6 and WP7
read data/nfl_lines_history.csv; WP7 may re-run
scripts/fetch_nfl_lines_history.py --seasons 1999-2025 --skip-espn for the
Elo warm-up (nflverse only). Nothing else touches the network.

Constraints: tests are Unix-only because moe.py imports fcntl — never
report them as passing locally. Do not push to GitHub and do not deploy
without asking me. Do not run the judge from this session or from any
interactive session; god-judge.timer owns judge runs. Keep the two-arms,
one-policy rule: the rating voice is a committee input registered like any
other expert, not a third arm; no policy versioning; the empirical table
is a policy switch (margin_model), not a new arm.

When all three report green: merge into main from the main repo directory
in the order WP5, WP6, WP7, resolve conflicts, remove the worktrees, run
the full suite once more on a fresh scratch clone, update the status log
in docs/god-expert-roadmap.md and the intake plan, then stop and give me:
what changed per WP, the test output, the Week 1 replay numbers from the
overlap and relevance tests (Seahawks pool margin from +4.2 toward +3.9;
Rams side edge from 4.4% toward 3.2%), the 2025 calibration check for the
empirical table, the Elo Brier against the closing moneyline on 2025, and
the deploy you propose (pull as root in /home/forwarder/app; restart
telegram-intake only if moe.py or intake_bot.py changed; the margin_model
switch stays "normal" until the calibration check is read). I decide the
deploy timing; the target is a week boundary.
```
