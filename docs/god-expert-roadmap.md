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

### WP10 — Refit on the ledger (data, ~50 graded games)

- Refit λ and veto sizes on live rows; compare with the backtest's values.

## Dependencies and parallelism

- Phase 1 (WP1–WP4) landed and was deployed on 2026-09-07 (status log).
  Built as four worktrees (`git worktree add ../telegram-forwarder-<slug>
  -b god/<slug>`), each with its own VPS scratch clone, merged in the order
  WP4, WP1, WP3, WP2; the one merge interaction was WP3's tests meeting
  WP1's veto on the default test market.
- Phase 2: WP5 can start now (WP1 is merged). WP5 touches `moe_god.py`
  heavily, so no other `moe_god.py` change runs beside it; WP6 and WP7 can
  run in parallel with each other and read WP4's files. Merge WP5 first and
  rebase WP6 on it (WP6 changes `cover_probability`/`over_probability`).
  Note for WP7: the committed CSV covers 2016–2025; the Elo warm-up on
  1999–2022 needs `scripts/fetch_nfl_lines_history.py --seasons 1999-2025
  --skip-espn` (about 7,000 rows) or a direct read of the nflverse file;
  decide whether the wider CSV is committed.
- WP8 needs WP6 and WP7. Its open→close calibration has 543 usable events
  (392 with movement): ESPN BET through 2025 week 12, DraftKings after.
- After each merge: full suite on a fresh scratch clone — eight modules now:
  `scripts.test_moe_god scripts.test_moe scripts.test_moe_ak
  scripts.test_moe_win_total scripts.test_generate_moe_opinion_cli
  scripts.test_intake_bot scripts.test_god_judge_runner
  scripts.test_nfl_lines_history` — then deploy at a week boundary (Tuesday
  after the Monday game is graded is the natural slot).

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

## Decisions

Decided 2026-09-07 in chat, recorded here and on the Desk page:

- Veto 0.5 points on spreads, 1.0 on totals, 10 cents on price; EV floor
  2% per unit. The backtest (WP8) may refine them.
- The human gate stays for every row, including the rating voice's (WP7).
- Judge runs are automated from a fresh headless session (WP2); the
  ensemble (WP9) follows once runner usage is measured.

Still open on the Desk page — read its `decisions` collection at the start
of every implementation session (Artifact `read_db`, collection
`decisions`): the four Week 1 approvals, the two Week 1 bets, the Rams
committee refresh, the voice selection rule (default stays "registry default
model, one row per expert"), stake language, grading cadence, the server's
dirty tree, and the two hidden rows. Week 1 rows stay pending until a human
approves them.

Open after phase 1 (2026-09-07) — the user's call, nothing in code assumes
an answer:

- `moe/prompts/god_rules/v1.md` step 7 omits the veto and the floor. Bump to
  v2 (re-hashes `prompt_sha256` on every future rules row) or leave.
- A veto knob of 0 vetoes every leg that has opening data. If 0 should mean
  disabled, the checks need `knob > 0`.
- The reason guard grounds a cited "N games" through the cohort a cited
  record implies (`17-8` → 25 games); the real Week 1 Rams response needs
  it. Keep, or drop and accept that row as an audit record.
- Judge call volume: the committee key includes the latest prices, so every
  BetOnline price move re-judges the game (cap 3 calls per pass, 144 a
  day). Read `logs/god_judge_runs.jsonl` for a week, or coarsen
  `committee_key` to lines only.
- `GOD_JUDGE_HEALTHCHECK_URL` is unset; `ping_hc` no-ops until it is added
  to the local `.env` and synced.
- WP6 must name its close: nflverse (the CSV, 2016–2025) or ESPN (the JSON,
  2024–2025 open/close). They differ by a point or more on 14% of spreads.
- Rejected judge rows re-run on the same committee key (session decision
  2026-09-07 in `scripts/god_judge_runner.py`); pending and approved rows
  block. Reverse it if a rejection should stay final.

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
