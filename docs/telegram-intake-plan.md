# NFL Telegram Lean Intake → Google Sheets — Plan

## Goal

Add a Telegram-based NFL lean intake system. A user DMs a dedicated intake bot
with `/guess_nfl_game`, the bot presents the available NFL games (read live from a
Google Sheet), and the user taps a game. The bot shows first-observed and latest
BetOnline spread, moneyline, and total prices from The Odds API, then requires the user to select a market and side
before entering a free-text opinion, including any line or price at which their
preference would change. The submission is appended to a **new dedicated intake
Google Sheet/tab**.

Reuses the Google Sheets access patterns proven in the `line-movement/` repo
(service-account auth + read/write rate-limit buckets + 429 backoff).

## Living-plan rule

This file is the authoritative implementation record. As work progresses, record
material decisions here with their rationale and status before or alongside the
code change. If implementation differs from an earlier proposal, update the
proposal rather than leaving contradictory guidance in place.

## VPS environment update rule

Never replace `/home/forwarder/app/.env` with a complete local file.

Every VPS environment change must:

1. Read the existing VPS `.env`.
2. Check whether the exact key already exists.
3. Replace only that key when present, or append it when absent.
4. Preserve every unrelated key and `.env.local`.
5. Write atomically, keep `forwarder:forwarder` ownership, and enforce mode
   `600`.
6. Validate required key presence without printing secret values.

This rule applies even when a local helper such as `syncenv` is unavailable.
Whole-file `scp`/install replacement is prohibited because a local environment
may not contain the VPS's complete production configuration.

## Implementation status

### Completed — 2026-08-04: NFL line data foundation

- Created a new Google workbook dedicated to this project; no sheets from
  `line-movement` are reused.
- Created `nfl_games`, `nfl_line_snapshots`, and `nfl_leans` tabs with structured
  headers.
- Configured a dedicated Google service account through base64
  `GOOGLE_CREDENTIALS`; the workbook ID is configured through
  `NFL_INTAKE_SHEET_ID`.
- Added `nfl_lines.py` and `scripts/fetch_nfl_lines.py`.
- Added focused coverage in `scripts/test_nfl_lines.py`.
- Completed a live BetOnline fetch and real Sheet write. The immediate repeat
  updated game freshness without appending duplicate snapshots.

### Next

- Observe the collector timer through at least one naturally due polling window.
- Capture the authorized Telegram user ID and begin the native prototype using
  the configured dedicated bot.

### Completed — 2026-08-04: Dedicated bot registration

- Created and validated the dedicated bot **NFL Guesser**
  (`@nflguesser_bot`).
- Stored its token only in untracked environment configuration as
  `INTAKE_BOT_TOKEN`; the token is not committed or documented here.
- Telegram `getMe` authentication succeeded.
- The four non-bot members of the intended private group are configured by
  numeric Telegram ID in untracked `.env` through `INTAKE_ALLOWED_USER_IDS`.
  The IDs themselves are not committed or documented here.

### Completed — 2026-08-04: Native intake prototype

Adopted slate behavior:

- `/guess_nfl_game` initially shows games in the next 10 days.
- `/suggest` opens a native ForceReply input for freeform product feedback. Each
  response is appended to the `suggestions` worksheet with UTC/ET timestamps,
  Telegram user identity fields, message ID, and suggestion text.
- Inline controls can expand the window to 30 or 365 days.
- Results are paginated so the 365-day view does not create an oversized
  Telegram keyboard.
- Selecting a game shows first-observed and latest BetOnline lines for the full
  game, first half, and first quarter.
- The detail view keeps full team names in the title, then uses team emojis in
  repeated spread/moneyline rows to reduce visual noise.
- Team-to-emoji mappings live in the `team_emojis` worksheet with `team_name`
  and `emoji` columns. The bot reloads the tab for each interaction, so worksheet
  edits apply consistently without a restart; unmapped teams display `🏈`.
- A TL;DR at the top derives BetOnline's implied Q1, halftime, and final score
  from each latest total and spread. Missing period markets display `nodata`.
- Game details use Telegram HTML formatting: Spread, Moneyline, and Total labels
  are underlined, and Opening/Latest labels are separated from their market rows
  by a blank line.
- Selecting a game now continues through button-only period, market, and side
  choices. Periods are full game, first half, and first quarter; markets are
  spread, moneyline, and total; valid sides are the two teams or over/under.
- After selecting a period, the bot displays that period's opening/latest
  spread, moneyline, and total before asking for the market. After selecting a
  market, it displays opening/latest values for both valid sides before asking
  for the side.
- After the structured choices, a ForceReply prompt captures the only free-text
  guess input: the lean, reasoning, and line/price where the preference changes.
- The final selected period/market/side and opening/latest prices are displayed
  in a Telegram blockquote, providing a native bordered summary immediately
  above the ForceReply prompt.
- Successful submissions append to `nfl_leans`, confirm the selected
  period/market/side, clear in-memory state, and restore the persistent command
  buttons.
- Every structured step has reverse navigation: side → market → period/game
  detail → game list. Returning from the free-text stage deletes and invalidates
  its ForceReply prompt so a stale reply cannot be submitted accidentally.

### Completed — 2026-08-10: Season-win prediction flow

- `/predict_nfl_wins` opens a 32-team picker backed by the seeded
  `nfl_win_totals`, `nfl_team_history`, and `nfl_win_predictions` tabs.
- Teams are ordered by the current user's number of submitted revisions,
  ascending, then by abbreviation. This keeps unmarked teams first while moving
  revised teams toward the end.
- A team view shows its current BetOnline total, the prior season's complete
  division standings, and places the selected abbreviation in parentheses.
- Historical context is shown separately for each of the last two transition
  cohorts. For each year it gives the following-season record of the team that
  previously occupied the same rank in the same division, plus the average and
  descending list of following-season win totals for same-ranked teams in the
  other seven divisions.
- If the user has a prior prediction for the selected team, it is displayed.
  The line is omitted entirely for an unmarked team.
- Inline buttons offer every integer from 0 through 17. The confirmation view
  compares the selection with the BetOnline total before saving.
- Saves append a per-team revision to `nfl_win_predictions` and immediately
  rebuild `nfl_win_predictions_latest`. Re-saving the unchanged latest value is
  acknowledged without adding a duplicate revision.
- After a save, the bot reports progress and links directly to the next
  unmarked team, or to the review screen after all 32 teams are complete.

### Implemented locally — 2026-09-04: MOE foundation and schedule expert

The first mixture-of-experts slice adds one independently versioned schedule
expert. It is intentionally manual until its real outputs have been reviewed.

Authoritative configuration lives under `moe/`:

- `moe/experts.yaml` contains stable expert identity, expert and prompt
  versions, mode, input profile, output schema version, and initial weight.
- `moe/prompts/schedule/v1.md` is the immutable first prompt. Its first live
  output inferred "Week 1" despite a null week field, so
  `moe/prompts/schedule/v2.md` explicitly prohibits numbered or relative week
  claims when week is unknown. A subsequent v2 attempt discussed the unavailable
  week cohort and was correctly persisted as invalid. V3 omits missing week
  fields and cohorts entirely and prohibits any week reference when absent. A
  structurally valid v3 output still introduced an unsupplied stadium name, so
  v4 explicitly prohibits external proper names and unsupported qualitative
  claims. V5 distinguishes the allowed calendar weekday from the unavailable
  NFL season week number, so phrases such as "day of the week" are allowed while
  numbered or relative season-week claims still fail. A temporary blanket
  validator for words such as "unusual" was removed because it also rejected
  quantified comparisons; unsupported qualitative claims remain part of the
  exact-text human review.
- User review of the first candidate required comparative evidence rather than
  isolated favorable splits. V6 supplies both home and road records for each
  team, every available month, and the exact NFL-week cohort. It instructs the
  expert to present symmetric counter-evidence, omit undefined strong/weak
  opponent claims, remove generic historical-data boilerplate, fold overlapping
  empty cohorts together, and treat confidence stars as expert-specific rather
  than cross-expert calibrated.
- V7 incorporates the completed human review format: concise plain-text bullets
  rather than tables, separate measured support and counter-evidence, a
  `no_signal_factors` list for empty cohorts, and a
  `discarded_considerations` list naming interpretations that were considered
  but rejected and why. The final opinion follows Pick / Why the pick / Why it
  may be wrong / No signal / Discarded considerations / Conclusion.
- V8 adds current-month venue cross-splits for both teams after review exposed
  that the source games supported September-at-home and September-as-away
  analysis but the input builder only supplied month and venue separately.
- V9/output schema v2 requires an exact integer away/home final-score
  prediction. The exact score is a representative outcome; expected margin
  remains a separate estimate, but winner, probability, margin direction, and
  score winner must agree.
- V10 follows a rejected Sonnet run that invented a team-level
  month-by-matchup-type split and treated the away team's home split as
  matchup-aligned evidence. Every numeric claim must now map to an explicit
  input field; only the home team's current-month home split and away team's
  current-month away split are matchup-aligned. Potentially useful schedule
  data that is absent from the input must be named under Discarded
  considerations with why it could matter, creating a visible review queue for
  future input and prompt improvements.
- V11 follows another rejected Sonnet run that transcribed a supplied 4-7
  record as 3-8 while retaining its 36.4% rate and claimed a best/worst result
  across monthly venue cohorts that were not supplied. It requires W-L-T
  arithmetic to be checked against the explicit sample fields and restricts
  superlatives to complete cohort sets at the same aggregation level.
- V12 follows a third rejected Sonnet run that still misranked the current
  month and mislabeled a venue-aligned overall away split. The input builder
  now deterministically supplies the current month's high-to-low win-rate,
  margin, and scoring ranks plus the number of compared months. The prompt must
  copy those ranks and explicitly distinguishes matchup-aligned overall venue
  roles from the more specific current-month venue fields.
- V13 follows a fourth rejected Sonnet run that inferred narrow wins/heavy
  losses from aggregate averages, introduced unsupplied schedule labels, and
  collapsed mixed current-month ranks into a broad strongest-month claim. It
  prohibits distribution claims without distribution data, requires exact
  rank language, and restricts schedule descriptions to literal supplied
  metadata. Missing potentially useful data must be named neutrally.
- After Sonnet v13 again inferred a win-only scoring claim from an aggregate
  five-game average, the Schedule Expert was made Opus-only by configuration.
  `default_model` and `allowed_models` are stored in `experts.yaml`; generation
  fails before inference if a caller requests a different model. The
  multi-model storage and Telegram picker remain generic for other experts.
- `moe/prompts/divisional/v1.md` defines the Opus-only Divisional Expert. It
  produces an opinion for every game. Divisional matchups compare each team's
  divisional baseline, divisional home/away roles, the recurring opponent pair,
  first/second meeting splits, sweep/split counts, and historical rematch
  performance conditioned on the first result. It also compares home-team
  results in first and second annual pair meetings at three levels: NFL-wide,
  within the current division, and for the exact opponent pair. These cohorts
  are explicitly labeled as historical home-side results, not the current home
  team's record. Opus may place each cohort separately under supporting or
  counterevidence according to its measured direction; it is not required to
  force conflicting cohort results into one conclusion. Generation validation
  requires the exact applicable record after the labels `NFL-wide home-side`,
  `<division> home-side`, and `Opponent-pair home-side`, so no level can be
  omitted. Validation also rejects unsupported ranking and significance labels
  such as "elite", "dominant", "outstanding", or "significant" because the
  input supplies no league rankings or statistical tests. Measured comparative
  terms such as "stronger" or "superior" are accepted only when the opinion
  includes the underlying records and numeric game samples. Non-divisional
  games compare
  non-divisional, conference, and non-conference performance against each
  team's divisional baseline.
- Divisional Expert output schema v3 replaces free-form reasoning strings with
  claim objects containing exact input evidence paths. Generation resolves and
  stores evidence snapshots, validates numeric claims and attribution, and
  renders `full_opinion` deterministically. Opus no longer writes a duplicate
  free-form opinion that can drift from its cited claims. Validation also
  rejects treating first pair meeting as early-season, all best/worst
  superlatives (while allowing the non-ranking idiom "at best"), and
  unsupported preparation/readiness narratives. Numeric
  validation normalizes Unicode minus/dash characters before comparing claims
  with cited values, accepts both leading-zero and leading-decimal rates, and
  tolerates floating-point noise at an otherwise valid rounding boundary.
- Discarded considerations are restricted to non-numeric unavailable concepts;
  supplied numeric evidence must use a cited claim object instead of bypassing
  validation through a free-form discarded string.
- Schema-v3 and schema-v4 generation allow up to two bounded validation-repair attempts.
  Each failed response is first persisted as its own invalid audit row; the
  next attempt receives the exact failed JSON and validator error. Validators
  remain unchanged, and every repair receives a new opinion ID.
- Missing-record validation errors include matching candidate input paths so a
  repair can add the exact omitted citation rather than guessing.
- A uniquely matching record path may be attached deterministically when the
  claim names the corresponding team or exact home-side cohort. Ambiguous
  records still fail and require repair.
- Divisional Expert output schema v4 removes factual prose from inference.
  Opus selects one ranked usable-evidence path list plus no-signal paths.
  Application code resolves those paths, classifies each usable card as support
  or counterevidence relative to the predicted side, and renders every team
  name, record, rate, margin, sample size, and cohort label deterministically.
  The model supplies only the pick values and path ranking. The overview thesis
  and conclusion are also rendered deterministically from the winner,
  confidence, and existence of retained counterevidence.
- Divisional prompt v20 requires complete selectable evidence-card or null
  paths and explicitly rejects parent containers and scalar children in the
  opinion path lists. For a divisional game, each team now has three
  deterministic, non-overlapping comparison cohorts: the current opponent,
  the rest of the division excluding that opponent, and non-divisional
  opponents. All six paths must be selected or explicitly marked no-signal.
  The broader `division_games` aggregate is optional context because it
  overlaps the first two cohorts and must not be counted as independent
  corroboration. Deterministic path normalization runs before the separate
  factuality inference, so a malformed opinion is audited and repaired without
  spending a factuality call.
- The same main inference may return freeform `nondeterministic_analysis`
  claims. A separate versioned factuality prompt classifies each exact claim as
  supported, reasonable inference, or unsupported against the same controlled
  input. Only supported claims and `Interpretation:`-prefixed reasonable
  inferences are appended to Telegram; unsupported claims remain audit-only.
  If the checker labels a claim usable but its cited paths fail deterministic
  validation, code downgrades that claim to unsupported rather than invalidating
  the deterministic opinion. Numeric discarded text is likewise excluded from
  rendered output while remaining preserved in the raw response.
- Factuality prompt v3 states that overall-team interpretations require the
  applicable parent path ending in `.all_games`; citing only scalar children
  does not satisfy that deterministic invariant.
- Expert registry entries may define exact-model `model_prompts` overrides.
  Prompt path, prompt version, output schema, prompt hash, and source hash are
  resolved after selecting the model, so persisted provenance always describes
  the instructions actually used. Models without an override continue to use
  the expert's shared prompt.
- Haiku 4.5 uses dedicated prompts rather than runtime edits to shared prompts.
  Divisional Haiku output schema v7 returns only forecast values and exact
  evidence-card paths; application code classifies and renders the cards and no
  freeform factuality pass is needed. Schedule Haiku output schema v3 returns
  concise cited claim objects and lets application code render the final
  opinion. Deterministic validation rejects explicit inverted comparisons and
  claims that mix overall month rankings with venue-specific month cohorts.
- Every model gets one initial inference and at most one fresh targeted repair.
  Failed attempts remain append-only audit rows; there is no retry-until-pass
  mode.
- The current ESPN schedule determines whether divisional opponents are in
  meeting one or two and the days between their two scheduled games. For a
  second meeting only, generation fetches completed current-season ESPN games
  and whitelists the exact first meeting's score, deterministic winner, and
  signed home margin. No other current-season score or record enters the expert
  input.
- Non-divisional expert inputs use the existing `nfl_game_history`
  `matchup_type` classification; no schedule-sheet migration is required.
  The Divisional Expert treats `division_games` only as an explicitly labeled
  comparison baseline and rejects divisional venue/meeting paths for
  non-divisional games. The Schedule Expert must first state the exact
  `division`, `conference`, or `non_conference` bucket, then receives both
  teams' `non_division_games` plus the exact `conference_non_division` or
  `non_conference` cohort for that matchup.
- Every persisted opinion records the prompt path and SHA-256, expert
  configuration SHA-256, repository commit, model, maximum output tokens,
  output schema version, exact input JSON, input SHA-256, raw response, whether
  the source tree was dirty, and a SHA-256 over the generation source files
  (including the AI transport and Sheets helpers).
- `moe/prompts/win_total/v5.md` defines the Opus-only Win Total Expert. Its
  whitelisted input compares the two current BetOnline season totals, every
  forecaster's latest season-win predictions with equal weighting, and each
  person's latest full-game moneyline pick against their own season ordering.
  Game picks include both direct `nfl_leans` rows and celebrity attribution
  from `celebrity_picks`. Each forecaster is cited separately, so an
  inconsistent moneyline pick cannot be hidden inside consensus. Historical
  analogs use prior-season wins only:
  exact away/home prior-win pairs, matching win-gap cohorts, and matching
  prior-win level buckets. They are explicitly not presented as historical
  bookmaker totals, because those are not stored. The expert reuses existing
  `nfl_win_totals`, `nfl_win_predictions`, `nfl_team_history`,
  `nfl_game_history`, and `nfl_leans` data, so no worksheet migration is
  required.

The schedule expert's enforced data contract is:

- Historical completed NFL final scores and margins from `nfl_game_history`
  are allowed.
- Current-season records, results, scores, injuries, news, rosters, rankings,
  and betting markets are prohibited.
- The current game input contains only event/season/week identity, kickoff
  calendar metadata, home/away teams, and derived matchup type.
- `nfl_games` currently leaves week blank for some Odds API events. Missing week
  metadata and exact-week historical evidence are omitted rather than guessed.
- Historical data is reduced to auditable team, venue-role, weekday, month,
  current-month-by-venue, week-number, head-to-head, and comparable-matchup
  summaries.
- The payload is built from a whitelist rather than by passing an `nfl_games`
  row to the model. A recursive guard rejects known market and live-state keys.

Every schedule opinion must provide an exact predicted winner, integer away and
home final scores, home-win probability, expected home margin, 1–5 confidence
stars, thesis, supporting factors, counterarguments, and a complete standalone
opinion. Validation rejects team-name mismatches, invalid ranges, tied or
winner-conflicting score predictions, contradictory
winner/probability/margin combinations, empty evidence fields, theses too long
for the paginated summary, and responses that would exceed the Google Sheets
per-cell character limit. It also rejects numbered or relative week claims when
the input week is null. Nothing is silently truncated.

`moe_opinions` is append-only and stores both normalized fields and the complete
input/output artifacts. `GoogleSheetsMoeOpinionStore` sits behind the
`MoeOpinionStore` protocol so generation and bot code are not coupled to
worksheet calls. `MOE_STORAGE_BACKEND=google_sheets` is the initial backend;
future SQLite migration can implement the same protocol.

Every model attempt is written by the generation function itself, rather than
by a later CLI step. Valid outputs use `generation_status=valid`; malformed or
validation-failing responses are persisted with their exact raw response,
`generation_status=invalid`, and an explicit error before the exception is
re-raised. Sheet appends use the existing quota/backoff helper. If Sheets still
rejects an append, the complete row and error are atomically appended with mode
`600` to `logs/moe_pending.jsonl`; `scripts/replay_moe_spool.py` retries those
rows after atomically rotating the active spool. New failures continue writing
to a fresh active file during replay, and Sheet writes are idempotent by
`opinion_id`. Replay also holds a non-blocking OS file lock so only one recovery
process can claim and replay a batch at a time. Spool writers hold a shared lock
on the same file while opening and appending; replay holds it exclusively across
rotation, reading, and cleanup, so a writer cannot append to an inode that the
replayer is about to unlink.

Structurally valid output is approved automatically at generation with
`reviewed_by=validation`. Invalid and sample rows remain excluded. Approval
metadata includes UTC timestamp, reviewer, note, and the hash of the exact
approved output, event, expert, prompt, model, source, and input identity. Bot
reads recompute that hash and hide any row changed or reassigned after
approval. `review: human` remains an explicit registry opt-out, and legacy
pending rows can still be reviewed manually:

```bash
python scripts/review_moe_opinion.py \
  --opinion-id <uuid> --status approved \
  --reviewed-by <reviewer> --note "<why it is safe to expose>"
```

The NFL game detail view now links to a paginated MOE summary. For each expert,
the summary selects the latest valid, approved opinion for the first available
model in this order: Fable, Opus, Sonnet, then Haiku. If none of those models
is available, it falls back to the latest approved model. Selecting an expert
with approved opinions from multiple models opens a model picker containing
the latest approved run per exact model ID in the same Fable, Opus, Sonnet,
Haiku order, followed by any other models by recency; a single-model expert
opens directly. Expert detail is paginated to stay below Telegram's
message-size limit and its footer shows the expert version, exact model ID, and
prompt hash. The main MOE summary also shows the exact model ID beside each
expert name. Opinion UUIDs bind model-picker callbacks without placing long
model names in Telegram's 64-byte callback payload. Newer pending, rejected,
invalid, or tampered runs never hide the latest approved run for a model. The
bot never generates an opinion during a user interaction. Event-bound views
are validated against the active game, so stale buttons fail closed instead of
showing another game's opinions.

Telegram read paths use process-local, lock-protected TTL caches to avoid Sheets
quota exhaustion during button navigation. `nfl_games` caches for 60 seconds,
the complete `moe_opinions` tab and `nfl_win_predictions` cache for 30 seconds,
`team_emojis` caches for 10 minutes, `nfl_win_totals` for one hour,
`nfl_team_history` for six hours, and the celebrity registry for five minutes.
Concurrent misses are coalesced under one refresh lock, and the authenticated
spreadsheet/store objects are reused. MOE callbacks route before the general
intake-data loader because the selected game is already held in Telegram state,
so repeated overview/detail/model navigation performs no game or emoji reads.
Win-prediction submissions still perform an authoritative duplicate read, then
write the updated rows through to the prediction cache so the submitting user
sees the change immediately. Celebrity creation similarly refreshes or updates
the registry cache under its write lock.
If a refresh receives an explicit Sheets 429 and a prior value exists, the bot
logs a warning, serves that stale value, and waits another TTL before retrying;
other API failures still surface.

Manual workflow:

```bash
# Inspect the exact schedule-only input without calling Claude:
python scripts/generate_moe_opinion.py \
  --event-id <nfl_games event_id> --expert schedule --show-input

# Explicit direct-API fallback:
python scripts/generate_moe_opinion.py \
  --event-id <nfl_games event_id> --expert schedule --api

# Compare the same expert using another model through the API fallback:
python scripts/generate_moe_opinion.py \
  --event-id <nfl_games event_id> --expert schedule \
  --model claude-opus-4-8 --api
```

There is deliberately no inference-only preview flag: every model call persists
its complete input and output. `--show-input` is safe because it does not invoke
the model.

All enabled experts use `claude-opus-4-8` with maximum reasoning effort. The
Divisional Expert's separate factuality request uses the same model and effort.
New rows persist `generation_backend` and `generation_effort`; approval hashes
bind both values when present while legacy approved rows without them retain
their existing hashes.

The Schedule Expert also permits `claude-fable-5` as an explicitly selected
comparison model while retaining Opus 4.8 as its default. The selected model is
recorded in the opinion row and approval hash.

Schedule Expert v26 also permits explicit `claude-sonnet-4-6` and
`claude-haiku-4-5` comparison runs at maximum reasoning effort. Opus 4.8
remains the default production model.

Schedule prompt v15 makes `game.matchup_type` label-only routing metadata. The
expert must state the exact bucket first for transparency, but its input omits
all division/conference performance cohorts, opponent-specific history, and
head-to-head evidence. The validator rejects matchup-structure reasoning after
the required label because that evidence belongs to the Divisional Expert.

The Divisional Expert likewise permits `claude-fable-5` as an explicitly
selected comparison model while retaining Opus 4.8 as its default. Its
independent factuality pass must use the same selected model and configured
effort.

Divisional Expert v33 also permits explicit comparison runs with
`claude-sonnet-4-6` and `claude-haiku-4-5`, both at maximum reasoning
effort. Opus 4.8 remains the default production model.

Schedule and Divisional Fable comparisons use model-specific `medium` reasoning
effort, while their default Opus 4.8 generation remains at `max`. Persisted
`generation_effort` records the effective selected-model value.

Agent-session generation is the preferred interactive workflow for every
registered expert. Direct application API generation remains available only
through the explicit `--api` flag.

The runtime-neutral canonical project skill at
`.claude/skills/generate-nfl-moe-opinion/SKILL.md` provides an explicit
alternative to API-key generation. GitHub Copilot discovers a wrapper under
`.github/skills/generate-nfl-moe-opinion/SKILL.md` that loads the same
workflow. A compatible agent runtime uses Opus 4.8 with maximum reasoning,
saves its raw JSON outside the repository, and passes it through the normal generator with
`--agent-response`. Schema-v4 experts require a second independently generated
factuality response via `--agent-factuality-response`. This path uses the same
input builder, validators, Sheet persistence, output hash, and human approval
gate as direct API generation; it records `generation_backend=agent_runtime`
and never approves an opinion automatically.

### Implemented locally — 2026-09-05: AK calibration expert

The AK Expert adds a market-calibration perspective based on AK's exact
projected score. It returns an independent full-game spread opinion and total
opinion; either may be `PASS`.

- Future AK submissions made as AK require a canonical away/home score and
  persist four append-only normalization fields in `nfl_leans`. Celebrity
  submissions, including ones entered by AK, keep the standard optional-score
  free-text flow. Prompt examples use team nicknames without city names
  (`Patriots`, `Seahawks`); the parser accepts those labels and still stores
  canonical full team names. Other intake users are unchanged.
- `moe_ak.py` builds a whitelisted schema-v5 input from AK's latest projection,
  the submission and current BetOnline markets, the last matching pre-kickoff
  snapshot, and completed historical outcomes. Reviewed normalized scores are
  authoritative; legacy prose is parsed conservatively and conflicting,
  ambiguous, tied, or missing projections are excluded.
- Side and total history are independently eligible. Missing spread data does
  not erase total evidence, and missing total data does not erase side
  evidence. Submission-line and closing-line records remain separate.
- `moe/priors/ak_wnba_v2.json` provides the reviewed WNBA cold-start prior.
  Side and total weights decay independently, are capped at two equivalent NFL
  observations, and expire at eight matching resolved NFL predictions. The
  source's provisional WNBA `±6` threshold is not copied as six NFL points:
  positive NFL gaps map as 0–<3 → WNBA 0–<6, 3–<6 → WNBA 6–<9,
  6–<9 → WNBA 9–<12, 9–<12 → WNBA 12–<16, and 12+ → WNBA 16+.
  Splitting the former middle band reveals materially different fresh results:
  WNBA 6–<9 finished Under 1/3 and supplies no directional warning, while
  WNBA 9–<12 finished Under 3/3 and retains the under warning. The neighboring
  supported records are Under 3/4 for WNBA 0–<6 and 2/2 fresh for WNBA
  12–<16; WNBA 16+ has no isolated reviewed sample.
- `moe/prompts/ak/v1.md` selects exact deterministic evidence IDs. Application
  code renders all factual cards, the combined thesis, and the complete detail
  text. A zero-NFL-sample recommendation using WNBA evidence is capped at one
  star; any recommendation using it is capped at two stars.
- The expert is pinned to `claude-opus-4-8` with maximum reasoning and supports
  both direct API and agent-runtime generation. Every attempt remains
  append-only, hash-bound, manually reviewed, and hidden until approved.
- `scripts/backfill_ak_predictions.py` defaults to report-only mode. Its guarded
  migration appends four `nfl_leans` columns and three `moe_opinions` columns
  only after exact-prefix validation. Applying historical normalized scores is
  a separate explicit action after human review, writes only `parsed` rows,
  skips identical prior writes, and refuses conflicting preexisting values.
- Telegram's main MOE summary, model picker, and detail view show AK's side and
  total separately while preserving the exact model ID.

The first Rams-49ers trial input resolves AK's projection as Rams 27-23 against
a Rams -4, total 48 submission market. That creates a zero side-margin gap and
a +2 total gap. No resolved AK NFL calibration observations exist yet; the
matching WNBA side prior is 4-2, while the total maps to the WNBA 0–<6 band
that finished Under in 3/4 fresh games.

### Implemented locally — 2026-09-09: Cee expert v4

The Cee Expert is a separate human-interpretation voice rather than an AK
variant. It produces a side-only opinion from Cee's final full-game moneyline
pick and, when available, final full-game spread pick. It treats the moneyline
as the outright-winner position and the spread as the expected-cover position
that informs the projected margin.

- The input is whitelisted and hash-bound. Each market carries its exact
  final selected side, rationale, submission-time market snapshot, and
  separately time-frozen season predictions. If both team projections did not
  yet exist when the spread was submitted, that context is explicitly marked
  unavailable rather than dropping the spread or blocking the expert.
- Every eligible pre-kickoff moneyline and spread revision is retained in
  chronological order under `decision_history`. Each market is classified as
  `initial_only`, `reaffirmed` (multiple submissions with no side change), or
  `changed` (at least one side transition, including a later change back).
  Counts, initial/final sides, exact rationales, and opaque submission
  references are supplied so the agent can distinguish a late reconsideration
  from repeated conviction without treating either pattern as inherently
  predictive.
- A deterministic `market_relationship` labels the positions `same_side`,
  `split_compatible`, `split_conflicting`, or `moneyline_only`. A favorite
  moneyline plus an underdog spread is compatible; an underdog moneyline plus
  the favorite's negative spread is conflicting because both cannot win.
- Rationale premises such as injuries or roster strength remain attributed to
  Cee and are never presented as independently verified facts. Numeric records,
  lines, and other statistics inside Cee's exact rationale are accepted as
  evidence only when the opinion cites that Cee submission or decision-history
  path; they remain explicitly attributed to Cee. Free-form prose from every
  other expert remains ineligible as numeric evidence. Exact Cee submission
  dates are likewise accepted from cited decision history rather than being
  misread as three-part betting records.
- Calibration uses only resolved, pre-kickoff Cee NFL moneyline picks and
  counts each historical game once using its final eligible submission. It
  reports the overall record plus matching season-order consistency,
  season-win-gap, and moneyline decision-pattern buckets. This measures whether
  prior initial, reaffirmed, or changed final moneyline decisions won
  outright. When a current spread exists, a separate matching spread
  decision-pattern bucket grades historical final spread revisions ATS at
  their submitted lines. There is no cross-sport prior.
- Cee validation accepts the unambiguous words `one` and `single` as a
  submission count of 1; other counts remain numeric and exact.
- Prompt v4 makes the schema's numeric provenance rules explicit: generated
  output scores are not repeated as cited input evidence, and uncited
  `discarded_considerations` contain no numeric tokens.
- Zero resolved calibration games cap confidence at two stars; one or two cap
  it at three.
- The expert uses output schema v3, participates only in the God Expert's side
  pool when an approved row is available, and supports Opus 4.8, Fable 5,
  Sonnet 4.6, and Haiku 4.5. It is committee-optional, so games without a Cee
  submission do not block the automated God Expert.

### Implemented locally — 2026-09-09: Hi Lo Expert v1 / prompt v2

The Hi Lo Expert is a committee-optional market-structure voice. It receives
no injuries, rosters, records, ratings, news, schedule-strength evidence, or
other expert opinions. It generates for every game: qualifying extremes drive
the opinion when present, while other games explicitly report that they have
no weekly extreme and describe their current line values and ranks.

- `moe_hi_lo.py` decodes the existing full-game, first-half, and first-quarter
  BetOnline lines. Weekly extrema are tie-aware and report the value, selected
  side, rank, tied event ids, distance to the next distinct value, and market
  coverage.
- A market board is eligible only when at least four games and at least half
  the week's games have that market. Sparse period boards remain visible in
  `weekly_market_positions` but cannot create an outlier opinion.
- The tracked categories are largest spread underdog, highest and lowest
  total, largest moneyline underdog, and largest moneyline favorite for each
  available period.
- Literal season extrema are computed from the season's stored game rows.
  Full-game weekly-extreme calibration is computed from the committed
  1999–2025 regular-season lines, including every tied extreme. Spread cohorts
  grade the underdog ATS; total cohorts grade Over for the weekly high and
  Under for the weekly low; moneyline cohorts grade the selected extreme side.
- Historical H1/Q1 lines paired with period scores are not stored. Those
  extrema are retained with `status=unavailable` and must be rendered as no
  signal rather than being assigned an invented record. Persisting quarter
  scores and building current-season period calibration remains the next data
  phase.
- The prompt and schema-v3 validator require the exact outlier categories,
  periods, values, sides, tie counts, board counts, historical selections,
  records, weeks, observations, and the period-data limitation. Confidence is
  capped at two stars below 10 historical observations and three below 30.
- The registry ID is `hi_lo`, default model Opus 4.8 at max effort, markets
  `[side, total]`, automatic validation approval, and
  `committee_optional: true`.

### Implemented — 2026-09-08: Celebrity Expert

The Celebrity Expert is an Opus 4.8-only, committee-optional voice
whose participant set is rebuilt independently for every game. Missing
celebrities are absent rather than counted as disagreement, and one participant
is explicitly an individual signal. Its objective is to find predictive
identity-specific agreement and disagreement permutations; the vote
distribution is descriptive context, not the goal.

- `celebrity_picks` retains its original attribution columns and adds canonical
  bet identity, market family, subject, stat, direction, line, price, a
  deterministic selection label, exact raw input, and UTC kickoff. Existing
  rows are losslessly expanded in place; standard spread/moneyline/total rows
  are enriched from their linked `nfl_leans` submission when needed.
- Standard markets remain available to every intake user. Celebrity mode also
  offers structured player-prop, team-prop, and other-market entry while
  retaining the exact reply. All distinct canonical bets remain; the latest
  revision wins only within the same celebrity, event, period, market family,
  subject, and stat.
- Celebrity custom entry also accepts ordinary free-form text. The existing
  NFL parser extracts each leg, including multiple legs from one teaser or
  parlay, while the exact original reply is retained on every row. Each leg
  receives a canonical side, total, team-prop, or player-prop identity and a
  distinct pick hash, so one source message can contribute both a side and a
  total signal without losing their shared rationale. The optional structured
  `Subject / Market / Pick` form remains available as a deterministic manual
  override. Free-form period normalization supports full game, both halves,
  and all four quarters; partial-game rows remain tracked but ungraded until
  compatible deterministic period results exist.
- The deterministic input separately reports the active picks, side and total
  distributions, individual records, pairwise agreement records, and each
  celebrity's record specifically when a pair disagreed. Exact-permutation
  history matches celebrity identity plus home/away or Over/Under roles, rather
  than merely matching the number of participants or the majority label. Only
  the latest pre-kickoff revision of each bet and results available before the
  target kickoff enter calibration. Full-game side, total, and team-total bets
  can settle from final scores. Partial-game, player-prop, and other picks
  remain tracked but ungraded until compatible deterministic results exist.
  Calibration records persist W-L-P counts and game totals but omit unused
  duplicate chronological-result strings; the evidence catalog already
  preserves every record the model can cite, keeping growing participant
  slates within the Google Sheets cell limit.
- The opinion may recommend the current full-game spread side and/or total, or
  PASS either leg. Props and other markets may be displayed or used as
  counterevidence but cannot directly support a game side or total. A
  single-celebrity or zero-calibration input caps confidence at two stars.
- The approved voice enters only the God pools for non-PASS legs. Games with no
  celebrity opinion remain eligible for the automated God Expert.

`emergency_migration.txt` now documents the implementation requirements,
lossless export/import format, one-day service freeze and SQLite cutover,
verification gates, backups, and rollback with post-cutover delta replay. It
must be reconciled with the final script names after the backend implementation
and rehearsed against a production export before use.

### Implemented locally — 2026-09-08: Desk group (Option B)

The Telegram surface for operating the committee: one private supergroup
with forum topics, the intake bot as admin, SS and AK as members with
identical rights. Design record: the "MOE on Telegram" options page
(`https://claude.ai/code/artifact/83938fa2-fc51-4fb0-b968-72fbda733c94`).
Option B was chosen on 2026-09-08 with two corrections from review: SS and
AK are peers on every surface, and "AK" the person is distinct from the AK
Expert voice — a committee member whose input is AK's projected scores, the
way the rating voice's input is finals. Nothing in the desk limits what
either person sees or does.

- `moe_desk.py` owns the model (`build_desks`), the renderers, the
  idempotent sync (`sync_desk`) and a thin Bot API transport (`BotApi`,
  `urllib`, one 429 honoured). It imports nothing from `moe` (`fcntl`), so
  `scripts/test_moe_desk.py` runs on Windows; the caller passes the
  hash-verified approved rows (`moe.approved_opinions`) in. `intake_bot.py`
  wires it: a sync task inside the bot process every
  `MOE_DESK_SYNC_SECONDS` (default 120, floor 15), the `desk:` callback
  branch, the `/start op_<opinion>` and `/start game_<event>` deep links,
  and reviewers paging through pending rows in the DM detail view.
- Topics and cards (as redesigned on 2026-09-08 after the first live pass,
  when 17 status boards plus cards for games with nothing to do read as
  clutter and the voices' opinions were on no card at all).
  🏈 Picks is the reading surface: one card per game once two voices are
  approved or a God arm is — the God line on top (both arms' short legs,
  "pending review" while the arms wait, "—" before they exist), then one
  line per approved voice (pick, probability, stars, projected score) in a
  fixed order, and every thesis (arms first, with up to three supporting
  factors and two counterarguments) inside a collapsed
  `<blockquote expandable>` each viewer opens on their own screen;
  👁 Full opinions deep-links to the game's MOE view in the DM. A pinned
  card lists only decided games (an approved arm) with their legs.
  📥 Review is the legacy/manual-opt-out to-do list: a card exists only while a game has rows
  worth a decision — a pending row that is the latest valid row for its
  expert and model (older drafts superseded by a newer row are hidden, since
  the aggregator only reads the latest approved row; audit and sample rows
  never count), God arms first, each with ✅ ❌ callbacks and a 👁 deep link
  into the tapper's own DM — and is deleted, silently, once nothing is left.
  The pinned queue card is three lines: what is to review per game, how
  many committees are complete, and which required voices still have no
  row. 📊 Scores: `scripts/moe_grade.py --notify` posts the digest there
  (`<pre>`, silent) when `MOE_DESK_SCORES_TOPIC` is set and falls back to
  the watchdog DM.
- Shared-message rules: Picks reading controls navigate by editing the existing
  shared game card and never create detail messages. Show full opinions keeps
  the summary visible, lists God Rules and God Judge first, then the approved
  voices; Refresh opinions invalidates only the opinion cache and redraws that
  card. A tap is answered at once because Telegram discards a callback answer
  after a few seconds. `nfl_games` and `moe_opinions` both use one-hour
  in-process caches; bot-based reviews patch the opinion cache immediately,
  while Refresh opinions is the explicit cache bust for externally generated
  rows. The sheet cache serves its last good value through 5xx and 429. Each
  manually reviewed target row is confirmed still pending with a single-row read
  (`GoogleSheetsMoeOpinionStore.fetch`), the cached rows are patched after
  the review so the cards re-render without a full tab read, the reviewer
  list is kept warm by the sync loop. ✅ ❌ are checked against the `reviewer` role in
  `allowed_users` (`moe_identity.resolve_role_user_ids`; both reviewers hold
  it, granted with `scripts/desk_setup.py --grant-reviewer`), re-read the
  sheet (a write never trusts the display cache), refuse anything not pending
  ("Already approved by AK."), run the store's hash-checked `review` signed
  with the tapper's display name, and re-sync the card at once. "✅ Approve
  both arms" appears only when both God arms are pending
  (`desk:okarms:<event>`, rules then judge) — bulk approval stays per game
  for the two arms and per row for the voices, as decided on the options
  page. Legacy pending and rejected rows are visible to both reviewers in the group
  and, through the deep link, in their DMs; the DM browser
  (`/guess_nfl_game` → 🧠) stays approved-only.
- Loud and silent. Every card post and edit is silent. Loud replies: under
  the picks card once per newly approved bet leg
  (`bet:<opinion>:<side|total>`), under the review card once per game when
  the judge lock (kickoff − 2 h, the runner's cutoff) is within
  `MOE_DESK_LOCK_WARN_HOURS` (default 2) and rows are still pending. The
  runner's own completion DM is silenced with `GOD_JUDGE_PENDING_DM=0`
  (`run_once(pending_dm=False)`); its failure and stall DMs are unchanged.
- State: `moe_desk_state.json` (gitignored; `MOE_DESK_STATE_PATH` to move
  it) holds message ids and content hashes per card, announcements and
  kickoffs; atomic writes; entries pruned three days after kickoff; games
  are frozen once they kick off. Unchanged content is never edited, a
  deleted card is re-posted, at most 15 new messages per pass (a first pass
  over a full slate spreads across a few passes), API errors are collected
  per card and retried on the next pass.
- Setup, every step from a shell (the VPS Claude session included): create a
  private group, convert it to a supergroup with Topics, add the bot as
  admin with Manage topics + Pin messages, send `/desk` in the group — the
  bot replies with the chat id (and the topic id when sent inside a
  topic) and says whether the group is a supergroup with Topics yet;
  commands reach a bot in groups even with privacy mode on — set
  `MOE_DESK_CHAT_ID`, then
  `scripts/desk_setup.py --create-topics` (prints the three topic keys),
  `--grant-reviewer <telegram_id>` for each reviewer, `--check [--post-test]`,
  restart `telegram-intake.service` (the journal says "Desk group enabled"
  or why not). Keys live in `.env` (synced — add them locally first, since
  `syncenv` deletes server keys absent locally): `MOE_DESK_CHAT_ID`,
  `MOE_DESK_REVIEW_TOPIC`, `MOE_DESK_PICKS_TOPIC`, `MOE_DESK_SCORES_TOPIC`,
  `MOE_DESK_SYNC_SECONDS`, `MOE_DESK_LOCK_WARN_HOURS`, `GOD_JUDGE_PENDING_DM`.
  Empty keys leave the desk disabled.
- Deferred, per the options page: replies under a card as row notes; a
  two-signature rule for the arms (one tap decides a row today); a
  `/scoreboard` command; the voice generation timer and the committee-key
  coarsening are separate work packages and the reason the desk is quiet
  or not.
- Tests: `scripts/test_moe_desk.py` (model, renderers, sync, state,
  transport; Windows), `ReviewerRoleTests` in `scripts/test_moe_identity.py`,
  `DeskReviewTest` in `scripts/test_intake_bot.py`, the pending-DM gate in
  `scripts/test_god_judge_runner.py`.

### Implemented locally — 2026-09-06: God Expert aggregator (rules + judge)

Two aggregator experts sit on top of the committee and ride the identical
validate → persist → approve → display rail. Design record: the God Expert
Bake-off page
(`https://claude.ai/code/artifact/092a9011-e22f-4022-8546-80cc6b2a9467`) and
its two companions. Both arms consume one input and one policy; only the
probability estimate differs between them.

- `god_rules` (`mode: aggregator`, `input_profile: aggregator`, output schema
  v8, `allowed_backends: [deterministic]`). No model. `moe_god.py` builds one
  input: one approved opinion per enabled non-aggregator expert (the latest
  approved row on the expert's registry `default_model`, else the latest on
  any model; the rule used is persisted per voice), the BetOnline
  opening/latest full-game market de-vigged pairwise, each voice's projected
  margin and total converted to cover and over probabilities through a normal
  model (`sigma_margin`, `sigma_total`), a weighted pool, the pool shrunk
  toward the market (`shrink_lambda`), and the per-expert scoreboard (Brier,
  ATS and O/U at close, leg record, leg CLV) graded from ESPN finals and the
  last snapshot before kickoff. Hedge weights
  `exp(-eta * resolved * (brier - mean_brier))`, clipped to
  `weight_floor..weight_cap`, activate once at least two voices have
  `weights_min_resolved` graded games; until then every weight is 1.0.
  `moe/prompts/god_rules/v1.md` is the versioned algorithm spec and is hashed
  like a prompt; no model reads it. The row records `model=deterministic`,
  `generation_backend=deterministic`, and an empty effort.
- `god_judge` (`mode: aggregator_judge`, same input profile and schema,
  `default_model: claude-fable-5-1`, `allowed_models: [claude-fable-5-1]`,
  `allowed_backends: [agent_runtime]`; `claude_headless` joined it on
  2026-09-07 for the timer). The judge reads a masked request
  (`moe_god.build_judge_request`): voices become `Voice A…` in an order
  shuffled by a seed derived from the full input's hash, lenses are described
  without naming anyone, factor lists are capped (`factor_limit`,
  `factor_chars`), and the scoreboard reaches it only as each voice's own
  track record. That request is the exact `input_json` persisted with the
  judge row; it carries `aggregator_input_sha256`, so the full input
  (persisted with the rules row) is recoverable. The judge returns only
  `home_win_probability`, `expected_home_margin`, `projected_total`,
  `key_reasons`, `counterpoints`, and `discarded_considerations`; reasons
  may cite only voice labels or `market`/`pool`/`scoreboard`. There is no
  `--api` path for the judge: the application transport caps output at 5,000
  tokens with no thinking configuration, and the judge is meant to bill the
  Claude Code subscription through the agent-runtime skill.
- Shared policy (`aggregator_policy` at the top of `moe/experts.yaml`, copied
  into every input so a parameter change changes the input hash): for both
  arms the application derives `p_cover_home` and `p_over` from the estimate,
  computes `edge = estimate − fair` for each side of the spread and the
  total, bets the larger edge when it reaches `edge_threshold` and the
  expected value at the posted price is positive, assigns stars by
  `star_edges`, and sizes `kelly_fraction` of full Kelly capped at
  `max_stake_fraction`; otherwise PASS with one star. The model never chooses
  a side, stars, or a stake. Rows persist the legs in `side_pick_json` and
  `total_pick_json` (the AK shape) and the feature summary, voice key, and
  judge labels in `calibration_summary_json`; `pick_market` is
  `side_and_total`.
- Coherence: probability exactly 0.5 or margin 0 is rejected from the judge
  and resolved by the market favorite in the rules arm; a probability/margin
  sign disagreement is rejected from the judge and clamped in the rules arm,
  recorded as a discarded consideration. Predicted scores are derived from
  the projected total and margin, never tied.
- Telegram: the two-leg detail layout now keys on
  `pick_market == side_and_total` instead of the literal AK expert id, so
  aggregator rows render like AK rows. Nothing else in the bot changed; the
  new experts appear as buttons once a row is approved.
- CLI: `scripts/generate_moe_opinion.py --expert god_rules --deterministic`
  computes and persists the rules opinion (no model). `--expert god_judge
  --show-input` prints the judge request for the agent-runtime skill, and
  `--agent-response … --model claude-fable-5-1 --generation-effort max`
  persists it. `scripts/moe_grade.py` prints the scoreboard and, with
  `--write`, appends graded rows to a `moe_grades` tab (one row per graded
  opinion, skipped when its opinion id is already present). Since
  2026-09-07 `moe-grade.timer` runs it daily with `--write --notify`
  ("Completed — 2026-09-07: Daily MOE grading timer").
- Bake-off protocol, pre-registered on the design page: both arms run on
  every game; the primary metric is Brier on the probability estimates,
  secondary is CLV on fired legs, units are reported but not decisive; the
  Week 6 agreement check (identical legs on 90% or more of legs → stop and
  keep rules); Week 18 decision with ties to rules; the mean of the two arms
  is scored as a free third row.
- Tests: `python -m unittest scripts.test_moe_god` (Unix only, since
  `moe.py` imports `fcntl`). Since 2026-09-07 the God Expert suite is the
  eight modules listed under "Completed — 2026-09-07: God Expert roadmap
  phase 1".

### Completed — 2026-09-07: God Expert roadmap phase 1 (WP1–WP4)

The four sections below are phase 1 of `docs/god-expert-roadmap.md`. All four
were deployed together on 2026-09-07 at 19:13 EDT (main `92a3151`): pulled as
root, `telegram-intake.service` restarted (`moe.py` changed),
`god-judge.service`/`.timer` installed and enabled (first pass 19:42 EDT),
`check_deploy_sync.sh` all in sync. The suite of record was 238 tests on a
fresh VPS scratch clone across `scripts.test_moe_god`, `test_moe`,
`test_moe_ak`, `test_moe_win_total`, `test_generate_moe_opinion_cli`,
`test_intake_bot`, `test_god_judge_runner`, `test_nfl_lines_history`.

### Completed — 2026-09-07: Market-move veto and EV floor (WP1)

- Four knobs joined `aggregator_policy` (`moe/experts.yaml` and `DEFAULT_POLICY`
  in `moe_god.py`), hash-bound like the rest: `veto_adverse_spread_points` 0.5,
  `veto_adverse_total_points` 1.0, `veto_adverse_price_cents` 10,
  `min_ev_per_unit` 0.02. `aggregator_policy()` requires non-negative numbers
  (bools rejected), coerces them to float, and holds `min_ev_per_unit` within
  0..1. `version` stays 1; the judge request's policy subset is unchanged.
- `build_market_block` now fills `movement_since_open` through the module-level
  `movement_since_open(opening, latest)`: the existing keys keep their values,
  `away_spread` joins them, and `home_spread_price`, `away_spread_price`,
  `over_price`, `under_price` give the move in bettor cents via `price_cents()`
  (`+p − 100` / `−p + 100`, so ±100 is 0: −110 → +100 and −105 → +105 are both
  +10). A missing opening value leaves its delta `None`.
- `apply_policy`: once a leg clears `edge_threshold` with positive EV, an
  adverse move vetoes it — the market moved away from the bet's side since
  open: the team's spread rose by ≥ `veto_adverse_spread_points` (home bet:
  `home_spread` delta ≥ +0.5; away bet: ≤ −0.5) or its spread price lengthened
  by ≥ `veto_adverse_price_cents`; an Over when the total fell by ≥
  `veto_adverse_total_points` or the over price lengthened, an Under when the
  total rose or the under price lengthened. Then `ev_per_unit` under
  `min_ev_per_unit` passes as `ev floor`. Comparisons are ≥ with a 1e-9
  tolerance; missing opening data never vetoes. Either case is a PASS leg
  (edge, probability, fair kept; one star; stake 0) whose note starts with
  `adverse move:` / `ev floor:` and renders as `Policy: …` like before.
- Every leg carries `pass_reason`: `None` for a bet or a plain sub-threshold
  pass, else `adverse move`, `ev floor`, or `no positive expectation at the
  posted price`. `_leg_from_json` and the bot ignore it.
- Inputs persisted before the price deltas (the Week 1 rows) replay:
  `apply_policy` recomputes the movement from `market["opening"]`/`["latest"]`
  when the price keys are absent and reads the four knobs with the module
  defaults when the policy predates them. Week 1 under the new policy:
  Seahawks −3.5 passes (`adverse move`: −110 → +100, +10 cents; EV would have
  been 0.0225), Over 44.5 passes (`ev floor`: edge 3.3%, EV 0.0194); 49ers
  +3.5 at −110 still bets (EV 0.039, 1.1u, ★); the Rams total stays a plain
  pass (edge 0.7%). With the knobs disabled the persisted legs reproduce
  exactly.
- Renderings: the full opinion's "Movement since open" line and the "Line
  movement since open" counterargument add non-zero price moves in cents; the
  counterargument now also appears when only prices moved.
- `moe/prompts/god_rules/v1.md` is unchanged; its step 7 does not mention the
  veto or the floor (open decision: bump to v2 or leave). A knob of 0 is
  accepted and vetoes every leg with opening data (0 is not "disabled").
- Tests: `PolicyTests` (line/price vetoes on both sides and both totals, the
  floor, fail-open without opening data, veto before floor, pass reasons,
  knob validation, cents arithmetic incl. the ±100 crossing),
  `MovementRenderTests`, `Week1ReplayTests` on
  `scripts/fixtures/god_week1/{sea,lar}_rules.json` (the four persisted Week 1
  rows, read from the sheet on 2026-09-07; canonical JSON of `input_json`
  reproduces `input_sha256`).

### Completed — 2026-09-07: Judge plumbing and the headless judge runner (WP2)

- `generate_opinion(..., input_payload=...)` accepts a prebuilt aggregator
  input; `scripts/generate_moe_opinion.py --input-file <path>` (valid with
  `--deterministic`, `--agent-response`, and `--show-input`) makes the file
  the state: the rules arm persists it verbatim, the judge persists the
  request derived from it, `--expected-input-sha256` is checked against that
  request, and the sheet's opinions, snapshots, and finals are not re-read.
  Both arms pin to one shown state; the judge no longer races the lines
  fetcher. `--input-file` with `--api` or a non-aggregator expert is refused.
  The file holds the full input (normalization needs the judge labels and
  voice names); the masked request is derived from it, which is how the
  roadmap's `--input-file <req>` is realized.
- `moe_god.committee_key`: SHA-256 of the sorted voice opinion ids plus the
  nine latest full-game fields, never the capture timestamp. It rides in the
  input (before the seed) and in the judge request; rows persisted earlier
  lack it and never match.
- Backend `claude_headless` (allowed for `god_judge` next to `agent_runtime`;
  effort override allowed) records that a row came from the timer.
- Reason guard: every `W-L`/`W-L-T` record and "N games" count a judge reason
  cites must appear in the request text or among the numbers it carries in
  structured form (projected scores, winner-vote split, track-record tallies,
  integer counts under count-like keys, a cited record's implied cohort
  size). Failure is a ValueError → invalid audit row. The rules arm's
  generated reasons are unguarded. Both Week 1 judge responses replay from
  `scripts/fixtures/god_week1/`; the Rams one needed the vote split (`2-2`)
  and the record cohort (`17-8` → 25 games) to ground.
- `scripts/god_judge_runner.py` + `god-judge.timer` (:12/:42): per upcoming
  game with a complete committee, outside two hours of kickoff, build the
  input and request into a temp dir, persist the rules arm (unless a valid
  row carries the key), one `claude -p` call (Fable 5.1, max effort, no
  tools, registered prompt as the system prompt, request on stdin, empty
  cwd, no sheet credentials in the environment), persist the judge row,
  delete the temp dir, DM the reviewer with the review commands. Dedupe by
  committee key; two invalid judge rows stop attempts on that key; at most
  `--max-games` (3) per pass; a failed call is logged and DMed, never
  retried (`run_god_judge.sh` is single-attempt). Usage per call in
  `logs/god_judge_runs.jsonl`. `--dry-run` persists and calls nothing.
  Isolation: `--safe-mode` by default (every customization off — CLAUDE.md,
  skills, plugins, hooks, MCP — with auth working normally, so the
  subscription OAuth token from `~/.claude/auth.env` is honored);
  `GOD_JUDGE_CLAUDE_ISOLATION=bare` exists but the CLI's help says `--bare`
  never reads OAuth. A judge row the reviewer rejected does not block a
  fresh run on the same committee key; a pending or approved one does. The
  lines fetcher timer is `OnUnitActiveSec`, not calendar-aligned, so ":12/:42
  after the fetcher" is approximate; the runner uses the latest captured
  lines.
- Skill runbook: the timer is the normal path; the manual fallback runs from
  a fresh session that has never printed unmasked rows, through the input
  file. Tests: `scripts.test_god_judge_runner` (stub claude), new classes in
  `scripts.test_moe_god`, two CLI tests.

### Completed — 2026-09-07: Disagreement report and mean-of-arms ledger row (WP3)

- `scripts/moe_grade.py` prints, after the scoreboard, the God Expert
  disagreement report (`moe_god.arm_pairs` → `disagreement_report` →
  `format_disagreement_report`): one block per game graded for both arms with
  each arm's Brier and their difference, then one line per leg (side, total)
  saying whether the arms agreed (same selection and line; two passes agree)
  and, where they differed, who was right — the arm that bet and won; against
  a lost bet, the arm that passed; two winners, two losers, or a push →
  neither. Totals: games, legs, agreement rate, the paired Brier difference
  (rules − judge over games where both Briers exist) with its standard error
  (sample std ÷ √n, blank below two games), and the disagreement record
  (rules / judge / neither). `--json` becomes `{"scoreboard", "disagreement",
  "mean_of_arms"}`; the scoreboard content is unchanged.
- Pairing: per event, the judge row whose masked request carries an
  `aggregator_input_sha256` equal to a rules row's `input_sha256` (both arms
  on one sheet state) is preferred and marked `linked`; otherwise the latest
  row per arm pairs up. Only events graded for both arms appear.
- Mean of arms (the bake-off's free third row): `mean_of_arms_results`
  averages the two rows' `home_win_probability`, `expected_home_margin`, and
  projected total (sum of predicted scores), runs the shared `apply_policy`
  on the rules row's persisted market and policy (keys added after the row
  was persisted take `DEFAULT_POLICY`, so Week 1 mean rows are graded under
  the veto and floor the arms never saw), derives scores the way
  `normalize_aggregator_opinion` does, and grades it with
  `grade_opinion_row`. Ledger only: expert id `mean_of_arms`, opinion id
  `mean:<rules_opinion_id>:<judge_opinion_id>` (stable, so the `moe_grades`
  opinion-id dedupe holds); `--write` appends these after the per-opinion
  rows. It never enters the registry, `build_scoreboard`, Hedge weights, or
  voice selection — it is not an expert. A rules row without a persisted
  input aborts the run rather than being skipped.
- Week 1 replay on `scripts/fixtures/god_week1/sea_*`: the pair links by
  hash; rules bet Seahawks −3.5 and Over 44.5, the judge passed both; the
  mean is p 0.623, margin 3.74, total 45.5 → side passes (2.9% edge), Over
  44.5 bets.
- Tests: `DisagreementReportTests` and `MeanOfArmsTests` in
  `scripts/test_moe_god.py` (Unix only).

### Completed — 2026-09-07: Historical NFL lines (nflverse + ESPN open/close) (WP4)

Free historical lines for the God Expert backtests (roadmap WP4), two
committed data files produced by one idempotent, resumable script.

- `scripts/fetch_nfl_lines_history.py` downloads nflverse's `games.csv`,
  keeps `game_type == REG` for `--seasons` (default `2016-2025`, a range or
  a list), maps team codes through `NFLVERSE_TEAMS` to the 32 canonical
  names (`OAK`/`SD`/`STL`/`LAR` fold into the current franchise; the map's
  values are asserted equal to `TEAM_ABBREVIATIONS`), and rewrites
  `data/nfl_lines_history.csv` (2,639 games, 324 KB) with columns season,
  week, gameday, weekday, gametime, espn_id, away_team, home_team, scores,
  home_spread, total, moneylines, spread prices, over/under prices,
  nflverse_spread_line. `home_spread` follows the BetOnline convention
  (negative = home favored); nflverse's `spread_line` is the opposite sign,
  verified on 2024 BAL@KC (`3`, KC −148, won 27-20), 2023 DET@KC (`4`,
  KC −198, lost 20-21), 2025 SF@SEA (`-2.5`, SF −135) and by the moneyline
  favorite in 2,731 of 2,746 games.
- ESPN core odds (`…/events/{id}/competitions/{id}/odds`) for every game of
  `--espn-seasons` (default `2024 2025`, 544 events) → `data/nfl_open_close.json`
  (497 KB, keyed by ESPN id, sorted keys, `indent=1`): provider, fetched_at,
  and `open`/`close`/`current` blocks in the repo's field names. The
  per-team `pointSpread.american` is home-relative and is the spread of
  record (top-level `spread` agrees in 544/544; `details` is
  favorite-relative). ESPN BET carries open+close for 464 games; the 79
  games of 2025 weeks 13-18 come from DraftKings (first provider with
  open+close); 2024 wk2 PIT@DEN has no pregame provider (live-odds providers
  are never selected). Plausibility guards null the 2023-style blocks where
  a price sits in the line field. Paced 0.75 s, 20 s timeout, four
  backoff retries, checkpoint every 25 games; stored ids are skipped unless
  `--refresh`; `--espn-limit N`, `--skip-espn`, `--games-csv` for offline
  runs. Append a season with
  `--seasons 2016-2026 --espn-seasons 2024 2025 2026`.
- Cross-check printed by every run: ESPN's close is within half a point of
  nflverse's for 85.8% of spreads and 74.9% of totals; seven near-pick'em
  games favor different teams (one ESPN close, 2024 wk1 MIN@NYG, contradicts
  ESPN's own moneyline). Both books' closes are kept — the CSV is nflverse's,
  the JSON is ESPN's; WP6 should name which close it fits (nflverse for depth,
  ESPN only for open→close movement is the suggestion).
- Tests: `scripts/test_nfl_lines_history.py` (44 cases, offline: real
  nflverse rows and trimmed real ESPN payloads under `scripts/fixtures/`).
  `.gitignore` ignores `data/*` except the two data files (and keeps
  `angles/data/` ignored, which the old unanchored `data/` rule covered).

### Completed — 2026-09-07: God Expert roadmap phase 2 (WP5–WP7)

The three sections below are phase 2 of `docs/god-expert-roadmap.md`, built
on 2026-09-07 in three worktrees (`god/overlap`, `god/margins`,
`god/rating`) by one subagent each, in parallel, from a fresh session, and
merged into main in the order WP5, WP6, WP7. The suite of record is 296
tests on a fresh VPS scratch clone across the eight phase-1 modules plus
`scripts.test_moe_margins` and `scripts.test_moe_rating`
(`bash scripts/godbuild_test.sh <slug> <dir>` runs all ten). Merge decisions:
`rating_elo` informs the side pool only (its total is the league scoring
rate); `god_rules/v2.md` step 3 describes the `margin_model` switch; WP5's
registry test lists the rating voice. **Not deployed** as of this section:
the user decides the timing at a week boundary, and the deploy must be
followed by generating and bulk-approving the week's rating rows before the
judge timer's next pass (roadmap, "Acceptance for the phase-2 deploy").

### Completed — 2026-09-07: Evidence overlap and per-market relevance (WP5)

Roadmap WP5: the pool no longer counts a table twice when two voices recite
it, and a voice enters only the markets its expert informs.

- Evidence. `moe_god.extract_evidence` turns every `W-L` / `W-L-T` token in
  a voice's full supporting-factor and counterargument lists (before the
  `factor_limit` cap) into a tuple `[W, L, T, games]`; the cohort size is the
  "N games" count in the same item nearest to the record (each count is
  claimed by its nearest record, so "14-11 (over 25 games) … 19-7" gives
  19-7 its own 26), else W+L+T. A four-word cohort label rides beside each
  tuple for reading. Tuples dedupe on first appearance; the block persists
  per voice as `evidence`. Known limitation: a scoreline ("won 23-20") reads
  as a record. Voices persisted before the block (the Week 1 rows) are
  re-extracted from their capped text when replayed (`voice_evidence`).
- Overlap. `evidence_overlap` is the pairwise Jaccard index of the tuple
  sets (0 when both are empty), keyed by voice id without the diagonal, and
  persists in the feature block as `overlap`. `overlap_adjusted_weights`
  ranks voices by id and divides each Hedge weight by one plus the summed
  overlap with the voices ranked before it; the feature block keeps the raw
  values as `hedge_weights` and the discounted ones as `weights`, which is
  what every pool uses and what the judge sees as `pool_weight` (with
  `hedge_weight` beside it). Two identical voices therefore pool as 1.0 and
  0.5; three as 1.0, 0.5, 0.33. The roadmap's "sum to about one voice" does
  not hold under this rank rule (see the roadmap note; open for the user).
- Relevance. Registry `markets` per non-aggregator expert (schedule and ak
  `[side, total]`, divisional and win_total `[side]`; absent means both;
  `voice_markets` validates a non-empty subset, and `load_registry` refuses
  a bad entry). The side pool (win probability, margin, cover probability,
  winner votes, their dispersion) averages the side-informed voices; the
  total pool (projected total, over probability) the total-informed ones;
  `feature_block.markets` lists both. An empty pool has nothing to shrink:
  its pooled values are `null` and the blend is the market expectation, so
  the only edge left is the asymmetry of the posted prices (0.5 against the
  fair over of 0.489 on a -105/-115 total); the opinion records it as a
  no-signal factor. With the current registry the synthetic test
  committee's total pool holds schedule (46) and ak (48) only, so its
  shrunk total is 45.75 and the Over clears the EV floor at 0.048.
- Judge request. `overlap` (label-keyed both ways), `hedge_weights`, and
  `markets` (membership lists in label order, never id order, which would
  reveal the alphabetical ids) join the feature block; each masked voice
  carries `markets` and `hedge_weight` next to `pool_weight`; `evidence`
  itself stays out (the factor text is already there). Every new field is
  added only when the input carries it, so a request derived from a
  pre-WP5 input — the Week 1 fixtures — is byte-identical to before.
  `committee_key` is unchanged.
- Renderings. The rules arm adds one `pool` counterpoint naming the pair
  with the largest overlap and the weight the discount left; voice lines in
  the full opinion show `markets side+total` and the voice's largest
  overlap; the pool line shows the side and total pool sizes;
  `calibration_summary_json` carries `hedge_weights`, `overlap`, `markets`.
- Prompts. `moe/prompts/god_rules/v2.md` (step 4 describes both discounts;
  step 7 now states the market-move veto and the EV floor, closing the WP1
  open item) and `moe/prompts/god_judge/v2.md` (the judge is told the pool
  already discounts shared evidence once and reads `overlap` as voice
  independence). Registry: both experts at `version: 2`,
  `prompt_version: 2`; v1 files stay for the rows that hash them.
- Week 1 replay (persisted voices re-pooled under the current registry):
  Seahawks — divisional and schedule share exactly `11-4/15`, `6-9/15`,
  `23-10/33`, `13-20/33` (4 of 16 distinct tuples, overlap 0.25), schedule
  weight 0.8, pool margin +4.25 → +4.21, total pool (ak, schedule) 47.11,
  home-cover edge 3.28% → 3.22%, over edge 3.3% → 4.9%. Rams — overlap
  divisional/schedule 0.11 (the 4-2 head-to-head and a coincidental 2-1
  over 3), divisional/win_total 0.10 (a coincidental 1-2 over 3), weights
  schedule 0.9 and win_total 0.91, pool margin +0.50 → +0.53, 49ers side
  edge 4.42% → 4.38%. The roadmap's +3.9 and 3.2% targets are not reached
  by the specified formula.
- Tests: `EvidenceTests`, `OverlapWeightTests`, `Week1OverlapReplayTests`
  in `scripts/test_moe_god.py`; `MovementRenderTests` now floors the Over at
  5% to keep the floor note on show; `RegistryTests` pins v2 and `markets`.

### Completed — 2026-09-07: Empirical margins (WP6)

The margin model behind every cover and over probability is now a policy
switch, `aggregator_policy.margin_model` (`normal`, the default, or
`empirical`), shared by both arms like every other knob. It is not a third
arm and there is no policy versioning. Roadmap WP6.

- `scripts/build_nfl_margins.py` builds `moe/priors/nfl_margins_v1.json`
  from `data/nfl_lines_history.csv` — nflverse closing lines, the close of
  record for this table (ESPN's open/close in `data/nfl_open_close.json` is
  not used) — for seasons 2016–2025, filtered explicitly so a wider CSV
  changes nothing. Per game `margin_residual = (home − away) + home_spread`
  (the actual home margin minus the market expectation −home_spread) and
  `total_residual = (home + away) − total`; both sit on the half-point
  lattice. Bins are one point wide and floor-based: bin k holds closing
  lines in [k, k+1), so −3.5 and −4 share bin −4 and totals 44 and 44.5
  share bin 44. Each bin stores n and the residual distribution as sorted
  `[value, count]` pairs; a bin is supported at ≥ `min_games` = 30 (21 of 41
  spread bins holding 2,494 of 2,639 games, 94.5%; 20 of 32 total bins
  holding 2,592, 98.2%). Output is sorted-key JSON with fixed formatting, so
  a rebuild on the same CSV reproduces the file byte for byte (117 KB).
  Standard library only: the VPS venv has neither numpy nor scipy.
- Lookup (`moe_god.parse_margin_table`, `empirical_survival`): P(cover) =
  P(r > t) + ½·P(r = t) with t = −(expected_home_margin + home_spread) from
  the bin of the latest home spread; P(over) likewise with t = total_line −
  projected_total from the bin of the latest total. The half-push mass keeps
  home + away and over + under at exactly 1, which `apply_policy` assumes.
  The survival is exact at lattice points and linearly interpolated between
  them, so an edge moves continuously with the estimate instead of jumping
  at every half point; it is 1 below a bin's lattice and 0 above it. Outside
  the table's support (a bin under 30 games, or a line with no bin) the
  normal model with the policy sigma answers, so every guard fails open to
  the old arithmetic. `cover_probability`/`over_probability` keep their
  three-argument normal form; the table arrives through `table=` and every
  call site (`voice_from_row` derived values, `build_feature_block` pooled
  values and `edges_if_shrunk`, `apply_policy`) resolves it from the policy
  through `margin_table_for`. A persisted policy without the key replays as
  `normal` (DEFAULT_POLICY merge, like the veto and floor knobs).
- Identity: under `empirical` the input carries a top-level `margin_table`
  block (path, sha256 of the committed file, schema and version, seasons,
  games, min_games, bin width) — `None` under `normal` — so the input hash
  changes with the table the way it does with the knobs; the judge request's
  policy subset names `margin_model` and repeats the block. Requests derived
  from inputs persisted before the switch are byte-identical to before (the
  Week 1 fixtures still reproduce). `normalize_aggregator_opinion` refuses
  an input whose recorded table hash is not the committed file's, and
  `moe._source_sha256` hashes the table file for the aggregator experts.
- Calibration check (stored in the file's `calibration` block and printed
  by every build): table fitted on 2016–2024 (2,367 games), scored on 2025
  held out (272 games) at thresholds −10..10 by 0.5, Brier and log loss of
  P(r > t) + ½P(r = t) against the realized residual, pushes at t skipped;
  `empirical` falls back to the normal model off support exactly as
  production would.

  | 2025 hold-out | empirical Brier | normal Brier | diff | empirical log loss | normal log loss | in supported bins |
  |---|---|---|---|---|---|---|
  | Spread, all games | 0.21611 | 0.21606 | +0.00005 | 0.62240 | 0.62245 | 244/272 (89.7%) |
  | Spread, supported bins only | 0.21653 | 0.21648 | +0.00005 | 0.62323 | 0.62328 | — |
  | Spread t = −7 / −3 / 0 / +3 / +7 | 0.1934 / 0.2348 / 0.2503 / 0.2455 / 0.2060 | 0.1954 / 0.2357 / 0.2500 / 0.2421 / 0.2056 | −0.0021 / −0.0009 / +0.0003 / +0.0034 / +0.0004 | | | |
  | Total, all games | 0.21947 | 0.21894 | +0.00053 | 0.62982 | 0.62885 | 266/272 (97.8%) |
  | Total, supported bins only | 0.22026 | 0.21973 | +0.00054 | 0.63150 | 0.63050 | — |
  | Total t = −3 / 0 / +3 | 0.2391 / 0.2505 / 0.2424 | 0.2377 / 0.2500 / 0.2409 | +0.0014 / +0.0005 / +0.0015 | | | |

  `min_games` sensitivity (grid-mean Brier, all games, empirical vs normal):
  30 → spread 0.21611 vs 0.21606, total 0.21947 vs 0.21894; 50 → spread
  0.21554 vs 0.21606 (85.3% coverage), total 0.21915 vs 0.21894 (89.7%);
  100 → spread 0.21598 vs 0.21606 (58.5%), total 0.21921 vs 0.21894 (77.9%).
  Reading: on one held-out season the one-point-bin table is a wash on
  spreads (a tie on Brier, a hair better on log loss, better below the
  market at −7 and −3, worse at +3) and slightly worse than N(0, 13.5) on
  totals; every difference sits inside the noise of 272 games. Nothing here
  argues for flipping the switch, so `margin_model` stays `normal`; the
  production table (all ten seasons) is committed for when the backtest
  (WP8) or a second season says otherwise.
- Week 1 replay under `empirical` (the persisted estimates, the same veto
  and floor): Seahawks −3.5 stays vetoed on the −110 → +100 move (edge 3.29%
  → 3.46%); Over 44.5 goes from a floored 3.3% edge to a plain 0.16% pass
  (p(over) 0.5222 → 0.4908); 49ers +3.5 grows from a 4.42% edge (EV 0.039,
  1.1u, ★) to 6.74% (EV 0.083, 2.3u, ★★); the Rams total stays a pass.
- Tests: `scripts/test_moe_margins.py` (build, lookup, switch validation,
  input identity, generation under `empirical`, the source hash, the Week 1
  replay, and the committed file recomputed from the CSV inside the test).

### Completed — 2026-09-07: Rating voice (Elo) and bulk review (WP7)

The first committee voice that is arithmetic rather than a model: an Elo
rating per team, registered as expert `rating_elo` and pooled like any other
approved opinion. It is a committee input, never a third God Expert arm, and
every row still passes the human gate — in bulk, one week per command.

- Registry: `rating_elo` (`Rating Expert (Elo)`, `mode: model`,
  `input_profile: rating`, output schema 9, `default_model: deterministic`
  so the aggregator's default-model rule selects its rows,
  `allowed_backends: [deterministic]`, `markets: [side]` — side only,
  decided at the merge under WP5's relevance masks, because the voice's
  total is the league scoring rate and must not dilute the total pool; the
  roadmap proposed side+total — enabled).
  `moe/prompts/rating_elo/v1.md` is the versioned algorithm spec, hashed into
  every row like a prompt; no model reads it. `VOICE_LENSES["rating_elo"]`
  describes the lens for the judge without naming the expert.
- Arithmetic (`moe_rating.py`, the FiveThirtyEight NFL form): adjusted gap
  `g = home − away + hfa`; `p(home) = 1 / (1 + 10^(−g/400))`; expected margin
  `g / points_per_elo`; each final moves the home team by `K · m · (S − p)`
  and the away team by the mirror, `m = ln(|margin| + 1) · 2.2 /
  (0.001 · winner_gap + 2.2)` (a favorite's blowout moves less; a tie moves
  nothing); ratings regress `regression` of the way to 1500 between seasons.
  Stars from |expected margin| at 3 / 7 / 10 / 14 points. The projected
  total is the league scoring rate (the mean 2025 total, 46.03) — the voice
  carries no total signal and says so in `no_signal_factors`; predicted
  scores come from `_scores_from_estimate`, never tied.
- Prior `moe/priors/nfl_elo_v1.json`, written by `scripts/fit_nfl_elo.py`
  (pure stdlib, ~9 s): warm-up 1999–2022 from the widened CSV, grid search
  on 2023–2024 minimizing the Brier of the pregame home-win probability
  (coarse 880 + fine 165 replays of the whole file) → K 19, hfa 32,
  regression 1/3; `points_per_elo` 21.99 by least squares of the margin on
  the adjusted gap over the same seasons; fit Brier 0.2224 over 544 games.
  Check on 2025, untouched by the fit: Elo Brier 0.2224 vs 0.2116 for the
  de-vigged closing moneyline (272 games), a gap of +0.0108 — the roadmap's
  within-0.01 target is missed by 0.0008 and the prior records
  `target_met: false`; margin RMSE 12.93 vs 12.27 for the closing spread. A
  fit-season probe of the margin-damping constant (1.5 … none) and the
  probability scale (300 … 600) moved the 2023–24 Brier by under 0.0003, so
  the plain form stays rather than tuning on the check season. (The fit
  Brier and the 2025 Brier both round to 0.222421 by coincidence: 2023 is
  0.2338 and 2024 is 0.2110.) End-of-2025 ratings lead with Seattle 1694,
  Denver 1666, Houston 1643, the Rams 1643, Buffalo 1642.
- Input (`build_rating_input(game, current_season_results)`): the prior's
  path, SHA-256, schema version and parameters ride in the input like the
  WNBA prior's; the end-of-2025 ratings are regressed once for the game's
  season (any season other than `through_season + 1` is refused — refit the
  prior each offseason), then this season's ESPN finals that kicked off
  strictly before the game are applied in kickoff order. Ratings are used
  as written to two decimals, so the input's numbers reproduce the
  estimate; an adjusted gap of exactly 0 leans home (0.5001, +0.01).
  `normalize_rating_opinion` requires every number in the response to equal
  the input's estimate — a hand-edited response is an audit row.
- `moe.py`: `DETERMINISTIC_MODES` (the rules aggregator and every
  `mode: model` expert) run only on the deterministic backend with
  `model=deterministic` and no effort; `DETERMINISTIC_RESPONDERS` maps the
  input profile to the function that writes the opinion; schema 9 →
  `normalize_rating_opinion`; `_source_sha256` adds `moe_rating.py` and the
  prior for the rating profile. The deterministic backend stays refused for
  agent experts.
- Commands: `python scripts/generate_moe_opinion.py --event-id <id> --expert
  rating_elo --deterministic` (or `--show-input`) for one game;
  `python scripts/generate_rating_week.py --season 2026 --week N [--dry-run]`
  persists one pending row per upcoming game of the week, skipping games
  that already have a valid pending or approved rating row on the same
  input hash (new finals change the input, so a later run adds a fresher
  row and the earlier one keeps its status); `python
  scripts/review_moe_opinion.py --expert rating_elo --week N [--season 2026]
  --reviewed-by <you>` prints the week's valid pending rating rows as one
  table (game, kickoff ET, winner, score, p(home), margin, stars, opinion
  id, input hash) and approves them through the store's hash-checked review
  only with `--approve`. The single-row mode is unchanged. Nothing approves
  on its own.
- Weekly procedure (Tuesday, after the Monday final is graded): generate the
  week, read the table, approve it. `god_judge_runner.committee_experts()`
  now lists `rating_elo`, so the judge runs for a game only after its rating
  row is approved; until the week's rating rows are approved the runner
  skips every game with "committee incomplete, no approved row for
  rating_elo".
- Changed 2026-09-08, user decision: every valid generated opinion is approved
  on validation, not by a person. `moe_god.review_policy` defaults to
  `validation`, with `review: human` retained as an explicit opt-out.
  `generate_opinion` approves a valid row in the same step that
  validated it — `review_status=approved`, `reviewed_by=validation`, a
  fixed note, `approved_output_sha256` = the output hash, so
  `approved_opinions` verifies it exactly like a human approval — and
  The bulk review mode remains for legacy rows, which are not migrated
  automatically. Invalid and sample rows remain unapproved; a
  bad-but-valid result is corrected by fixing its root cause and regenerating.
- Data: `data/nfl_lines_history.csv` widened to 1999–2025 (6,967 games,
  807 KB; the 2016–2025 rows are byte-identical to the earlier file;
  moneylines start in 2006 and are complete from 2010, spreads and totals
  from 1999). `DEFAULT_SEASONS` in `scripts/fetch_nfl_lines_history.py` is
  now `1999-2025`, so a re-run is idempotent. The prior stores the CSV's
  SHA-256; the tests fail with "re-run scripts/fit_nfl_elo.py" if the CSV
  changes under it.
- Tests: `scripts/test_moe_rating.py` (Elo arithmetic, replay order and
  regression, scores, the least-squares slope, a grid-fit round trip, the
  committed prior reproduced from the CSV including the 2025 Briers and
  the 32 end ratings, input determinism and the strictly-before-kickoff
  replay, the season guards, the exact-offset tie break, response
  normalization and rejections, registry entry, generation end to end on
  the deterministic backend, backend guards, voice selection and the
  masked judge request with five voices, the bot renderings, the weekly
  generator's dedupe and dry run, the bulk review filters, table, approval
  and argument validation). `scripts/test_god_judge_runner.py`'s committee
  now carries an approved rating row (a complete committee needs one);
  `scripts/test_nfl_lines_history.py` pins the wider default.

### Completed — 2026-09-07: God Expert roadmap phase 3 (WP8–WP10)

The three sections below are phase 3 of `docs/god-expert-roadmap.md`, built
on 2026-09-07 (night) in three worktrees (`god/backtest`, `god/ensemble`,
`god/guard`) — the first two by forked agents in parallel, the third the
orchestrator's fix for two live judge rejections — and merged in that order
with no conflicts. The suite of record is 335 tests on a fresh VPS scratch
clone across the ten phase-2 modules plus `scripts.test_moe_backtest`
(`bash scripts/godbuild_test.sh <slug> <dir>` runs all eleven; 340 with
`scripts.test_moe_grade` after the merge with the daily grading timer).
**Not deployed**: phase 2 went live on 2026-09-07 at 21:06 EDT from another
session (with the daily `moe-grade.timer`, main `7a89d41`; its 17 Week 1
rating rows are pending approval), and phase 3 deploys on top of it
(roadmap, "Acceptance for the phase-3 deploy", which also covers the
server's uncommitted tree and the unit change).

### Completed — 2026-09-07: Backtest harness (WP8)

`moe_backtest.py` (library, standard library only, no `moe` import so it
runs on Windows) and `scripts/backtest_god.py` (`grid`, `veto`, `clv`,
`ledger`; global `--json`) replay the God Expert rules arm over historical
lines and score it. Nothing here is a copy of the production arithmetic:

- Path. A historical market goes through `moe_god.market_block_from_lines`
  (the body of `build_market_block`, factored out; `build_market_block`
  now decodes the packed BetOnline columns and calls it; an absent opening
  leaves every movement delta `None`, so nothing is ever vetoed), the
  rating voice through `moe_rating.rating_estimate` (the estimate block of
  `build_rating_input`, factored out; the input's `estimate` is that
  function's return) and `moe_god.voice_from_row` on a synthetic
  `rating_elo` row, the pool and shrink through `build_feature_block` and
  `rules_arm_response`, the legs through `apply_policy`, the grades through
  `_leg_result`. A change to any of those changes the backtest with it.
- Committee. The rating voice alone (the model voices have no history):
  Elo with the committed prior's parameters (K 19, hfa 32, regression ⅓,
  21.99 points per Elo), replayed from 1999 so every pregame rating is
  leak-free; the projected total for a season is the previous season's
  league scoring rate. The registry says `markets: [side]`, so the total
  pool is empty and the blend total is the market line: under `normal` no
  total ever fires, under `empirical` only the bin's own asymmetry at the
  line can clear a low threshold. `sigma_total` and the total veto are
  therefore not identifiable from this committee. The Elo parameters were
  fitted on 2023–2024 (in-sample for the fit window); 2025 is untouched by
  both fits.
- Market. `grid`: the nflverse close with juice (`data/nfl_lines_history.csv`),
  no opening (the veto is inert). `clv`: the ESPN open as the market and
  the ESPN close as the closing line (`data/nfl_open_close.json`,
  2024–2025, 543 of 544 events), so every fired leg carries closing-line
  value. The empirical model is evaluated without leakage: the table for a
  season is built from 2016 up to the season before it
  (`scripts.build_nfl_margins.build_table` + `moe_god.parse_margin_table`,
  applied through the new `moe_god.margin_table_override` context manager,
  backtests only; `check_margin_table` still refuses a foreign hash on a
  live row).
- Scores. ML Brier of the arm's `home_win_probability` against the winner
  (ties excluded, as `grade_opinion_row` grades) beside the de-vigged
  market's and the raw Elo's, log loss as a secondary; cover Brier of
  `p_cover_home` against the ATS result at the close (pushes excluded)
  beside the fair cover probability's; fired legs W-L-P, flat one unit at
  the posted price, ROI, mean CLV where a closing line exists, the record
  per star bucket.
- Grid and selection. λ {0, 0.25, 0.5, 0.75, 1} × σ_margin {12, 13, 13.5,
  14, 15} × margin model {normal, empirical} × edge threshold {0.02, 0.03,
  0.04, 0.05} × EV floor {0, 0.01, 0.02, 0.03} × two star ladders (the
  default shape and one with even rungs; the first rung is always the
  edge threshold, as the policy validation requires) = 1,600 policies,
  each validated through `aggregator_policy`, over 544 games in about 9 s;
  the estimate is computed once per λ and `apply_policy` once per policy.
  Selection, printed with its rule: λ by the lowest fit-season ML Brier;
  σ_margin and the margin model by the lowest cover Brier at that λ; edge
  threshold and EV floor keep the registry values unless a candidate beats
  them on ROI (and mean CLV where it exists) with at least 50 fit-season
  bets; the star ladder keeps the default unless the alternative is
  monotone in ROI where the default is not. The check seasons then score
  the chosen policy and the registry policy side by side, untouched by the
  selection. The script prints the resulting `aggregator_policy` block and
  never writes `moe/experts.yaml`.
- Result (fit 2023–2024, 544 games; check 2025, 272 games):

  | λ (fit ML Brier) | 0 | 0.25 | 0.5 | 0.75 | 1 |
  |---|---|---|---|---|---|
  | rules arm | 0.20944 (= market) | 0.21086 | 0.21350 | 0.21735 | 0.22242 (= Elo) |

  | Policy | Window | ML Brier arm / market / Elo | Cover Brier arm / fair | Legs | Units (ROI) |
  |---|---|---|---|---|---|
  | registry (λ 0.5) | fit 2023–24 | 0.2135 / 0.2094 / 0.2224 | 0.2531 / 0.2505 | 274, 133-134-7 | −10.54 (−3.8%) |
  | chosen (λ 0) | fit 2023–24 | 0.2094 / 0.2094 / 0.2224 | 0.2500 / 0.2505 | 2, 0-2-0 | −2.00 |
  | registry (λ 0.5) | 2025 untouched | 0.2156 / 0.2121 / 0.2231 | 0.2564 / 0.2496 | 122, 58-63-1 | −9.04 (−7.4%) |
  | chosen (λ 0) | 2025 untouched | 0.2122 / 0.2121 / 0.2231 | 0.2499 / 0.2496 | 4, 2-2-0 | +0.10 (+2.5%) |
  | registry (λ 0.5) | ESPN open 2024–25 (`clv`) | 0.2118 / 0.2099 / 0.2172 | 0.2536 / 0.2510 | 235, 104-127-4 | −29.98 (−12.8%), CLV +0.07 |
  | chosen (λ 0) | ESPN open 2024–25 (`clv`) | 0.2100 / 0.2099 / 0.2172 | 0.2500 / 0.2510 | 35, 18-17-0 | +1.98 (+5.7%), CLV −0.81 |

  At the registry λ = 0.5 (for reading, no selection): cover Brier prefers
  a wider σ (12 → 0.2538, 13.5 → 0.2531, 15 → 0.2526; `empirical` ≈ 0.2599
  at every σ); edge/floor 0.02/0 → 354 legs −17.6u (−5.0%), 0.02/0.03 →
  242 legs −4.6u (−1.9%), 0.03/0.02 (registry) → 274 legs −10.5u (−3.8%),
  0.04 → 230 legs −13.0u (−5.7%), 0.05 → 158 legs −4.9u (−3.1%); star
  buckets ★ 116 legs −4.9%, ★★ 98 −10.2%, ★★★ 48 −0.3%, ★★★★ 8-0,
  ★★★★★ 1-3 (not monotone; the alternative ladder is monotone and
  negative everywhere); on 2025 ★ +12.3%, ★★ −15.0%, ★★★ −34.3%.
- Reading. **`aggregator_policy` is unchanged.** The selection's λ = 0
  says the Elo voice adds nothing beyond the closing line at any weight —
  the same fact as WP7's 0.2224 vs 0.2116 — and that the registry policy
  on a rating-only committee loses units in every window. It says nothing
  about the live committee's four model voices, which have no history to
  replay; the roadmap's "fitted values written into `aggregator_policy`"
  waits for the ledger refit (WP10) on ~50 games of the real committee.
- Veto calibration (`veto`; ESPN open → close, 2024–2025, 543 events, all
  of which moved in some field; ESPN totals move in whole points, so the
  0.5 and 1.0 rows coincide). The side that got cheaper since the open,
  graded at the close, flat one unit at the close price; negative units
  mean the veto removes losing legs:

  | Move | ≥ 0.5 | ≥ 1 | ≥ 1.5 | ≥ 2 |
  |---|---|---|---|---|
  | spread (points) | 392 legs, 206-186, −2.84u (−0.7%) | 336, 172-164, −10.58u (−3.2%) | 176, 88-88, −9.69u (−5.5%) | 142, 69-73, −11.20u (−7.9%) |
  | total (points) | 403, 213-190, +2.80u (+0.7%) | same | 189, 107-82, +14.52u (+7.7%) | 187, 106-81, +14.62u (+7.8%) |

  | Price move (cents) | ≥ 5 | ≥ 10 | ≥ 15 | ≥ 20 |
  |---|---|---|---|---|
  | adverse side | 831 legs, 386-445, −74.41u (−8.9%) | 394, 175-219, −47.40u (−12.0%) | 149, 63-86, −22.92u (−15.4%) | 54, 23-31, −7.40u (−13.7%) |

  The harness's own rule proposes `veto_adverse_spread_points` 0.5 → 2.0
  (the 0.5–1 band is net positive for the adverse side) and keeps the
  other two. Decision: all three stay at the values decided on 2026-09-07
  — the spread gap is inside one standard error at 142 legs, the total
  veto's sign is reversed on this sample (no support either way), and the
  price veto is the one clear signal at its current 10 cents. Recorded as
  open items in the roadmap.
- Tests: `scripts/test_moe_backtest.py` (21): `market_block_from_lines`
  equals `build_market_block` on the test game and rebuilds the Week 1
  fixture market; `rating_estimate` equals `build_rating_input`'s estimate
  including the tie-break leans; hand-built scoring cases (Brier, units,
  CLV, pushes, star buckets); every grid point validates and the ladder
  rule holds; a season's empirical table holds only earlier seasons; the
  veto table on synthetic open/close; the ledger mode on the fixtures; the
  JSON is deterministic; on the real data 2023–2024 = 544 games, 2025 =
  272, and the raw Elo 2025 Brier reproduces the prior's 0.2224.
  `scripts/backtest_god.py` reconfigures stdout to UTF-8 (the reports carry
  star glyphs; a cp1252 console crashed the first Windows run).

### Completed — 2026-09-07: Judge ensemble (WP9)

Roadmap WP9, built default-off because it starts only after two weeks of
measured single-sample usage.

- Runner: `scripts/god_judge_runner.py --samples N` (`GOD_JUDGE_SAMPLES`,
  1–5, default 1; `run_once(..., samples=)`). With N = 1 the persisted rows
  and messages are byte-identical to before (the runs-log line now also
  carries `sample: 1, samples: 1`). With N ≥ 2, per game after the rules
  row: N sequential `claude -p` calls in the game's temp directory, each
  logged with `sample`/`samples`; a `JudgeCallError` is logged and the
  loop continues. No call returned text → the existing stage-`claude`
  failure (one DM listing every call error, no row). Otherwise every
  response persists through `generate_opinion(sample=True)`; the valid ones
  feed `ensemble_response`, whose JSON persists as the one judge row of the
  trigger (`claude_headless`, max, Fable 5.1, `expected_input_sha256` =
  the request hash) → `valid`/`pending`. Zero valid samples → the FIRST
  response persists as an ordinary invalid judge row (stage
  `judge_validation`), so the two-invalid-rows stall counts the trigger
  exactly as before. The dedupe and stall logic is untouched: sample rows
  are neither `valid` nor `invalid`. The pending DM reads `judge <id> (mean
  of k of N samples)` plus a `sample failures:` line when a call or sample
  failed; sample ids never get review commands.
- `moe.generate_opinion(..., sample=True)`: a validated row persists as
  `generation_status="sample"`, `review_status="not_applicable"` (output
  hash set, raw response kept); a failed one persists as
  `generation_status="sample"` with `generation_error`, review
  `not_applicable`, and the ValueError propagates as today. Nothing else in
  the row differs from a normal judge row. `approved_opinions`,
  `latest_opinions` (the bot's display), `review_moe_opinion.week_rows`,
  and `GoogleSheetsMoeOpinionStore.review` already ignore anything not
  `valid`; `SampleRowTests` pins each.
- `moe_god`: `coherent_estimate(probability, margin, *, fair_home)` is the
  fence tie-break and sign clamp factored out of `rules_arm_response`
  (same wording, `FENCE_NOTE`/`SIGN_NOTE`; both Week 1 rules fixtures
  reproduce numbers and notes exactly). `ensemble_response(samples, *,
  fair_home, size)`: means of the valid samples' three numbers rounded
  4/2/2 → `coherent_estimate`; reasons copied from the sample closest to
  the mean (round-6 |Δp|, then |Δmargin|, |Δtotal|, then call order) with
  the coherence notes appended to its discarded considerations; an
  `ensemble` block `{size, valid, samples, estimates, reasons_from, rule}`
  where `estimates` keeps each sample's own numbers. `_validate_ensemble`
  (exact key set, positive ints with `valid ≤ size`, `valid` unique
  non-empty ids, `valid` finite triples, `reasons_from` among the samples,
  a non-empty rule) runs on judge responses only — the rules arm still
  rejects the field — and the block lands in
  `calibration_summary_json["ensemble"]` (that key is now always present,
  `null` on every row built without a block, both arms); the full
  opinion's Blend line ends ` · mean of k of N samples`.
- Deploy: `deploy/systemd/god-judge.service` `TimeoutStartSec` 3600 → 9000
  (3 games × 3 samples × 900 s), README row updated; `cp` + `daemon-reload`
  at deploy. Start the ensemble with `GOD_JUDGE_SAMPLES=3` in `.env` +
  `syncenv` (config present on both machines; `.env.local` also works but
  is for server-only secrets) at a week boundary. Usage so far in
  `logs/god_judge_runs.jsonl`: 3 calls on 2026-09-07/08, 76–108 s API time,
  `total_cost_usd` 0.35–0.63, 6.9–9.5k output tokens (5.6–8.2k thinking),
  one turn each. The manual fallback in the skill stays single-sample.
- Tests: `EnsembleTests` and `SampleRowTests` (`scripts/test_moe_god.py`),
  `EnsembleRunnerTests` (`scripts/test_god_judge_runner.py`; the stub takes
  a per-call mode list and a `vary` mode answering 0.58/+2/44, 0.64/+4/46,
  0.61/+3/45): three samples → three sample rows and one valid mean row
  (0.61/3.0/45, the block naming the three ids, the DM text, three log
  lines, dedupe on the next pass); one invalid among three → mean of two;
  all invalid → one invalid judge row per trigger and the stall after two
  triggers; one crash among three → mean of two with the error in the DM;
  N = 1 → no sample rows; `--samples 0/6` rejected; the env honored.

### Completed — 2026-09-07: Ledger refit tooling (WP10) and the reason-guard fix

- WP10 waits for data (~50 graded games). Its tool is
  `scripts/backtest_god.py ledger`: persisted `god_rules` rows (valid and
  approved, or `--include-pending`) replayed under the WP8 grid on their
  own persisted market — opening and latest are both there, so the veto
  fires here — with each row's voices re-pooled under the live registry
  (`markets` added when the row predates it, as the Week 1 replay tests
  do) and a policy persisted before a knob taking that knob's default;
  graded through `_leg_result` against ESPN finals and `closing_market`
  from the snapshots; the same scores and selection; a per-knob veto sweep
  that turns one knob on at a time and lists the legs it removes and how
  they graded; a comparison against WP8's fitted values (`--grid-json`).
  Inputs from the sheet and ESPN on the VPS (reads only) or from
  `--rows-json/--finals-json/--snapshots-json`. Below `MIN_REFIT_GAMES`
  (50) the report is labeled informational. On the Week 1 fixtures with
  synthetic finals (20-27, 24-20): 2 graded rows, informational; under the
  registry policy the rows re-pool under WP5 (Seahawks 0.6258 vs the
  persisted 0.6259), Seahawks −3.5 is vetoed on the price move (the sweep
  attributes it to the price knob at 5 and 10 cents, never to spread or
  total), Over 44.5 clears the floor and wins, 49ers +3.5 wins, the Rams
  total stays under the bar → 2-0-0; the two-game selection is noise by
  construction.
- Reason guard (`moe_god`). The first two live headless judge responses
  (the 19:42 and 20:12 EDT passes) were rejected and stalled the Seahawks
  committee: "implied totals 24.0-20.5" matched `_RECORD_PATTERN` as the
  record `0-20`, and the AK voice's 21-27 projection written as "27-21"
  was not among the derived reference strings. The pattern, shared with
  the evidence extractor, now refuses a digit-dot before or a dot-digit
  after a record (`(?<!\d\.)…(?!\.\d)`; "13.5-14" and "0.5-1.0" were the
  same class of false positive), and `reason_reference_text` derives both
  score orders. No Week 1 fixture text changes under the tighter pattern,
  so the pinned evidence sets and overlaps are unchanged. Tests:
  `test_decimal_fragments_are_not_records`,
  `test_home_first_projected_score_is_grounded`, and the extractor case in
  `EvidenceTests`. The stalled key itself stays stalled until the
  committee changes; the fix applies to the next fresh key.

### Completed — 2026-09-08: Phase 3 deployed; approval hash fix for deterministic rows

Phase 3 (WP8–WP10, the reason-guard fix, rating rows approved on validation)
was merged with the Cee/Celebrity commits and the desk group and deployed as
main `6434045` (396 tests on a VPS scratch clone; `god-judge.service`
reinstalled for `TimeoutStartSec=9000`; `telegram-intake` restarted). The
first `generate_rating_week.py --season 2026 --week 1` then failed with
"Opinion content changed after generation; review refused" on every Week 1
rating row: deterministic rows persist `generation_max_tokens` 0, which the
in-memory row and the typed sheet read both hash as an empty string
(`0 or ""`), while the Sheets store's `review` reads raw cells and got the
string "0". No rules or rating row could be approved through the store —
CLI, weekly command or desk button — until `opinion_output_sha256`
normalised the field (`_max_tokens_text`: blank and zero are one spelling,
other values keep their integer text). Stored hashes are unchanged because
generation always hashed the empty spelling. Test:
`ApprovalHashNormalizationTests` in `scripts/test_moe.py`.

### Implemented locally — 2026-09-04: authoritative NFL week metadata

`nfl_games.week` previously remained blank because `new_game_row()` hardcoded
an empty value and line updates never supplied one. The Odds API does not expose
NFL season week metadata.

The line collector now fetches ESPN's official NFL season calendar once per
season represented in a fetch and assigns weeks by exact kickoff containment in
the published start/end ranges:

- Regular-season values are NFL Weeks 1–18, not calendar-year week numbers.
- Preseason periods are resolved independently from ESPN's preseason calendar,
  so preseason Week 1 cannot be confused with regular-season Week 1.
- Every game must match exactly one range. Missing or ambiguous matches fail the
  collector before any Sheet write.
- `write_to_sheets()` rejects any `GameLines` object without an authoritative
  week, preventing the blank-week regression from returning through another
  caller.

`scripts/backfill_nfl_game_weeks.py` validates the entire live tab before writing
column D and then re-reads it to ensure no blank weeks remain. The 2026 backfill
mapped all 67 existing rows: preseason weeks 1–4, regular-season Week 1
(16 games), Week 2 (1 game), and Week 12 (1 game). A second preview reported
zero rows requiring changes.

### Implemented locally — 2026-09-04: complete 2026 NFL schedule

`nfl_games` remains the odds-backed table and therefore contains only games
currently published by BetOnline. The complete season is stored separately in
`nfl_schedule`, preventing blank market columns from entering the bot's game
browser.

`nfl_schedule.py` fetches ESPN events from both calendar years touched by an NFL
season, keeps regular-season type 2, and stores event ID, NFL season/week,
status, UTC/ET kickoff, teams, neutral-site flag, and source. Validation requires:

- Exactly 272 unique regular-season games.
- Every NFL Week 1 through 18.
- Exactly 32 teams with 17 games each.
- A nonblank authoritative week on every row.

`scripts/setup_nfl_schedule.py --season 2026 --apply` created the live tab and
re-read all 272 rows through those invariants. The 18 regular-season games also
present in `nfl_games` were independently matched by teams and kickoff and all
18 week values agreed.

Prototype issue log:

- **Telegram menu command syntax:** Bot API command names allow letters, digits,
  and underscores, but not hyphens. `setMyCommands` rejected `intake-nfl`.
  Resolution: `/guess_nfl_game` is now the single spelling used by the persistent
  keyboard, command menu, handlers, documentation, and tests.
- **VPS `.env` overwrite:** the collector deployment copied an incomplete local
  `.env` over the production file, removing existing Telegram and application
  keys from disk. The running listener still held the original values in its
  process environment, so the production file was reconstructed from a strict
  allowlist of those live application keys, merged with the new collector keys,
  minified for safe parsing, backed up as `.env.pre-recovery-20260804`, restored
  with mode `600`, and copied back locally. No service was restarted while the
  file was incomplete. Future updates must follow the key-level rule above.
- **Telethon session helper prompted for a phone:** entering
  `TelegramClient` as a synchronous context manager auto-started authentication
  before the bot token was supplied. The helper was changed to construct the
  client, call `start(bot_token=...)` explicitly, save the session, and
  disconnect without using the auto-starting context manager.
- **Total line displayed with an odds sign:** the first real game-detail test
  rendered a total of `35` as `+35` because spread points, American odds, and
  totals shared one formatter. Totals now use unsigned line formatting while
  spreads and prices retain explicit signs.

### Completed — 2026-09-07: Daily MOE grading timer

- `moe-grade.timer` → `moe-grade.service` → `run_moe_grade.sh` →
  `scripts/moe_grade.py --write --notify`, daily at 05:23 ET (the VPS
  clock is America/New_York: after the last Thursday/Sunday/Monday night
  final, before the 06:00 auto-reboot and ~06:31 unattended-upgrade
  windows). Grading is deterministic — no model call, no Telethon — so
  one run costs four Sheets reads, two ESPN scoreboard fetches, and one
  append when there is something new. The ledger dedupes on opinion id,
  so every run is idempotent and the daily pass is the whole latency
  budget: finals land three or four nights a week and approvals can lag
  a game by days, hence daily rather than game days only; an idle run is
  four reads and an exit. `Persistent=true`, `TimeoutStartSec=1200`
  (two `_call_with_retry`-wrapped Sheets calls at ~6 min worst case).
- `--notify` DMs the operator through the watchdog bot
  (`send_watchdog_dm`, the judge runner's helper) only when rows were
  appended: the season header with the ledger delta, the finals those
  rows cover (deduped, capped at 20), and one scoreboard line per expert
  (`notification_text`, kept under Telegram's 4096 characters). It is a
  no-op without `--write` or without new rows. A failed DM after a
  successful append exits non-zero so the healthcheck `/fail` ping
  carries the log tail — the append never repeats, so the DM is the
  operator's only signal. `MOE_GRADE_HEALTHCHECK_URL` in `.env`;
  `ping_hc` no-ops unset.
- The judge runner recomputes the scoreboard live from finals and
  snapshots for every input, so this ledger feeds humans and the
  disagreement report, never the Hedge weights or voice selection.
- Units in `deploy/systemd/` (the drift check picks them up
  automatically); the runner lives at the repo root like the others.
  Tests: `scripts/test_moe_grade.py` (Unix only, like the rest of the
  suite).

### Completed — 2026-08-04: VPS collector deployment

Deployment checklist:

1. Commit and push the fetcher, tests, runner, timer, and living-plan updates.
2. Sync `ODDS_API_KEY`, `GOOGLE_CREDENTIALS`, and `NFL_INTAKE_SHEET_ID` to the
   VPS through the repository's existing environment workflow.
3. Pull the commit on the VPS.
4. Install `nfl-lines-fetcher.service` and `nfl-lines-fetcher.timer`.
5. Enable the timer and trigger one manual service run.
6. Confirm service logs, timer state, no-op cadence behavior, and workbook
   integrity.

Issues and deviations encountered during this phase will be recorded below
before the phase is marked complete.

Deployment issue log:

- **Stale local clone:** the local checkout was 225 commits behind
  `origin/main`, while the VPS already matched the current remote head. The
  collector work was stashed, `main` was fast-forwarded to `b2ed682`, and the
  work was reapplied without conflicts before committing.
- **Initial push denied:** Git was authenticated as
  `sahirboghani_microsoft`, which has read-only access to this repository. The
  collector commit remained intact locally; authentication was switched to the
  previously approved `sboghani1` collaborator account before retrying.
- **`syncenv` unavailable locally:** the documented helper was not installed or
  defined in the current shell environment. Its behavior was reproduced
  explicitly: `.env` was uploaded to a temporary VPS path and atomically
  installed as `/home/forwarder/app/.env` with `forwarder:forwarder` ownership
  and mode `600`; `.env.local` was not touched.

Deployment outcome:

- Collector implementation committed as `5fae910` and pushed to `main`.
- VPS checkout fast-forwarded to the collector commit without modifying its
  pre-existing unrelated dirty/untracked files.
- Installed and enabled `nfl-lines-fetcher.timer`; it is active and wakes every
  30 minutes.
- The timer's automatic run and a manual oneshot run both exited successfully
  with `No NFL games are due for polling; no API calls made.`
- An explicit live preseason fetch was run as the `forwarder` user to exercise
  VPS Odds API and Google Sheets credentials. It fetched one upcoming game,
  updated its existing `nfl_games` row, and appended zero duplicate snapshots.
- Workbook verification after deployment found 19 game rows, 19 unique event
  IDs, zero duplicate game IDs, and 20 legitimate movement snapshots.
- Required VPS environment keys are present:
  `ODDS_API_KEY`, `GOOGLE_CREDENTIALS`, and `NFL_INTAKE_SHEET_ID`.

This phase is complete. The next implementation phase is the dedicated native
Telegram intake bot.

---

## User flow (end-to-end experience)

The walkthrough below is an NFL example: the user asks for the current slate, the
bot lists the games, the user selects a game, and the bot shows opening and latest
prices. The user then selects a market and side before submitting a lean,
rationale, and any price-dependent conditions.

```
 ┌──────────────────────────────────────────────────────────────┐
 │  Chat with  @IntakeBot                                        │
 ├──────────────────────────────────────────────────────────────┤
 │                                                              │
 │                                    ┌───────────────────────┐  │
 │                                    │  /guess_nfl_game      │  │  ← user
 │                                    └───────────────────────┘  │
 │                                                              │
 │  ┌────────────────────────────────────────────────┐          │
 │  │ 🏈  NFL — available games                        │          │  ← bot
 │  │ Which game do you want to predict?              │          │
 │  │                                                 │          │
 │  │  ┌───────────────────────────────────────────┐ │          │
 │  │  │  Dolphins @ Bills      · Sun 1:00 PM ET   │ │  ◄ tap    │
 │  │  ├───────────────────────────────────────────┤ │          │
 │  │  │  Ravens @ Bengals      · Sun 1:00 PM ET   │ │          │
 │  │  ├───────────────────────────────────────────┤ │          │
 │  │  │  Packers @ Bears       · Sun 4:25 PM ET   │ │          │
 │  │  ├───────────────────────────────────────────┤ │          │
 │  │  │  Cowboys @ Eagles      · Sun 8:20 PM ET   │ │          │
 │  │  └───────────────────────────────────────────┘ │          │
 │  │        (inline keyboard — one button per game)  │          │
 │  └────────────────────────────────────────────────┘          │
 │                                                              │
 │                                    ┌───────────────────────┐  │
 │                                    │ (taps “Dolphins @ Bills”)│  ← user
 │                                    └───────────────────────┘  │
 │                                                              │
 │  ┌────────────────────────────────────────────────┐          │
 │  │ 📋  Dolphins @ Bills — Sun 1:00 PM ET           │          │  ← bot
 │  │ ─────────────────────────────────────────────   │          │
 │  │   Spread :  Open BUF -3.5 · Latest BUF -2.5     │          │
 │  │   Total  :  Open 47.5 · Latest 46.5             │          │
 │  │   ML     :  Open MIA +155 · Latest MIA +130     │          │
 │  │ ─────────────────────────────────────────────   │          │
 │  │ Enter your prediction:                          │          │
 │  │                                                 │          │
 │  │   Market ▾          Your lean                    │          │
 │  │  ┌───────────────┐ ┌───────────────────────────┐│          │
 │  │  │ Spread      ▾ │ │ Dolphins +2.5. Prefer +3  ││  ◄ type   │
 │  │  │───────────────│ │ or better; ML at +140.    ││    +      │
 │  │  │ Spread        │ └───────────────────────────┘│    input  │
 │  │  │ Moneyline     │        [ Submit ]             │          │
 │  │  │ Total         │                               │          │
 │  │  │ Other         │                               │          │
 │  │  └───────────────┘                               │          │
 │  └────────────────────────────────────────────────┘          │
 │                                                              │
 │                                    ┌───────────────────────┐  │
 │                                    │ Market: Spread        │  │  ← user
 │                                    │ Side: Miami Dolphins  │  │
 │                                    │ Prefer +3 or better…  │  │
 │                                    └───────────────────────┘  │
 │                                                              │
 │  ┌────────────────────────────────────────────────┐          │
 │  │ ✅  Logged your prediction                       │          │  ← bot
 │  │   Dolphins @ Bills · Spread · Miami Dolphins    │          │
 │  │   “Prefer +3 or better; ML at +140”             │          │
 │  │   → saved to intake sheet                       │          │
 │  └────────────────────────────────────────────────┘          │
 │                                                              │
 └──────────────────────────────────────────────────────────────┘
```

### How the “dropdown + input box” maps to Telegram

Telegram DMs have **no native side-by-side dropdown-with-textbox widget**. The
initial implementation will deliberately prototype the closest native experience
before deciding whether a hosted form is justified:

- **Native (v1, adopted):** inline buttons select the market and side; tapping
  them opens a `ForceReply` input box where the user enters their rationale and
  any line/price conditions. This prototype will use realistic NFL line movement
  so the team can evaluate the experience before adding hosting.
- **Telegram Web App (later):** a real HTML form renders the dropdown and text box
  exactly as drawn, submitting both fields at once. Requires hosting + a bot
  domain; natural upgrade if the form grows.

### Input UX invariant

The user should never need to type commands, dates, games, periods, markets, or
sides.

- `/start` installs persistent reply-keyboard buttons labeled
  `/guess_nfl_game` and `/suggest`.
- The Telegram command menu also exposes `/guess_nfl_game` and `/suggest`.
- Slate windows, pagination, games, periods, markets, and sides use inline
  buttons.
- Free-text input is limited to the final lean/rationale flow and the explicit
  `/suggest` feedback flow. Commands and all structured game choices remain
  button-driven.

Text-transcript form of the same flow:

```
User: /guess_nfl_game
Bot:  "NFL — available games:"     [inline keyboard, one button per game]
        [ Dolphins @ Bills · Sun 1:00 PM ] ...
User: (taps "Dolphins @ Bills")
Bot:  "Dolphins @ Bills — Sun 1:00 PM ET
       Spread: Open BUF -3.5 | Latest BUF -2.5
       Total:  Open 47.5     | Latest 46.5
       ML:     Open MIA +155 | Latest MIA +130
       Choose a market:"  [ Spread ][ Moneyline ][ Total ][ Other ]
User: (taps "Spread")
Bot:  "Choose a side:"             [ Miami Dolphins ][ Buffalo Bills ]
User: (taps "Miami Dolphins")
Bot:  "Miami Dolphins spread — enter your lean, reasoning, and the line or
       price at which your preference changes:"                 [ForceReply]
User: (replies) "Dolphins +2.5. Prefer +3 or better; ML instead at +140."
Bot:  "✅ Logged. Dolphins @ Bills · Spread · Miami Dolphins …"
      → row appended to the intake sheet
```

---

## Architecture

```
intake_bot.py            ← new: dedicated Telethon bot, command/callback/reply handlers,
                            in-memory conversation state, allowlist gate
intake_sheets.py         ← new: read game list + game info from source sheet(s),
                            append submission to the intake sheet
                            (adapts line-movement/sheets_utils.py patterns)
deploy/systemd/
  telegram-intake.service ← new: runs intake_bot.py under the forwarder user
run_intake_bot.sh        ← new: venv launcher (mirrors run_grade_daemon.sh)
```

### Design decision — process isolation

**Options considered:**
- **A. Separate process/systemd service (adopted).** A standalone, dedicated
  Bot-API-only intake bot, like `grade_daemon.py`.
- **B. Add handlers to the existing `listener.py` bot.** Reuse the already-running
  bot client and event loop.
- **C. Extend the existing Telegram Channels Claude bot** (the tmux `claude`
  session) to handle intake.

**Decision: A.** The forwarder `listener.py` runs a persistent Telethon
**user** session and is flood-wait sensitive (`CLAUDE.md`: "Deploy cautiously.
Rapid bot session restarts trigger Telegram flood waits"). Adding stateful,
frequently-iterated command handlers there means every intake code change forces
a listener restart, risking flood-waits and forwarding downtime. Option C couples
intake to an interactive AI session that has no history/backfill (dropped-message
risk noted in `CLAUDE.md`) and isn't a deterministic form handler. Option A keeps
intake **Bot-API-only** (zero Telethon-user/session risk, exactly the isolation
rationale behind `grade_daemon.py`), independently deployable and restartable, and
a crash/flood-wait on either side can't take down the other.

**Trade-off accepted:** one more service, bot token, session, and identity to
manage. This avoids update-polling ambiguity and allows a custom name and profile
picture without coupling intake to the forwarding bot.

### Design decision — bot framework

**Options considered:**
- **A. Telethon (recommended, adopted).** Already the repo standard
  (`requirements.txt: telethon>=1.42.0`), with bot-session tooling
  (`scripts/get_bot_session.py`) and established patterns in `listener.py`.
- **B. `python-telegram-bot` / `aiogram`.** Popular, higher-level conversation
  and keyboard abstractions (e.g. PTB `ConversationHandler`).
- **C. Raw Bot API over HTTP** (`httpx`, already a dep) with manual long-polling.

**Recommendation: A.** Telethon already provides everything the flow needs —
inline buttons (`Button.inline`), `events.CallbackQuery`, and `events.NewMessage`
with `ForceReply` — with **no new dependency** and a session pattern the team
already operates. B would add a dependency and a second mental model for the same
capability; its `ConversationHandler` is nice but our 3-step flow is small enough
that in-house state is simpler than mixing frameworks. C means reimplementing
update parsing/keyboards by hand for no benefit.

### Design decision — form UI mechanism

**Options considered:**
- **A. Inline buttons for game choice + `ForceReply` for the text prediction
  (recommended, adopted).**
- **B. Numbered text menu** ("reply 1–8 to choose a game").
- **C. Telegram Web App / custom keyboard form.**

**Decision: A for the prototype.** Inline buttons give an unambiguous, tap-to-select game
choice (no parsing of "which game did they mean"), and the **prediction type**
(`Total`/`Spread`/`Moneyline`/`Other`) is a second inline-button row — the native
stand-in for the dropdown in the mockup — after which `ForceReply` opens a reply
box we can positively match via `reply_to`, capturing exactly the prediction and
not unrelated DMs. B is brittle (users mistype, indexes drift if the list changes
between prompt and reply). C (Web App) is the only way to render a true
dropdown-plus-input side by side, but it needs hosting, a bot domain, and JS —
beyond the native prototype. After the prototype is used with realistic NFL
examples, the team will decide whether the improved UX justifies hosting.

---

## Data model

### `nfl_games` — current bot-facing state

One row per Odds API `event_id`, updated in place. The tab has 20 columns:

- Event metadata: event ID, season/type/week/status, UTC/ET kickoff, teams, and
  bookmaker.
- `opening_captured_at` and `latest_captured_at`.
- Three packed opening columns: away, home, totals.
- Three packed latest columns: away, home, totals.
- `last_updated_at` and `period_last_checked_at`.

The six market columns use the same positional format as the snapshot tab. This
keeps bot reads small while retaining all full-game, first-half, and
first-quarter markets.

### `nfl_line_snapshots` — append-only movement history

Append one row only when a market payload differs from the latest persisted
payload for that event. The tab has 12 columns: seven identity/time columns,
three packed market columns, and two API-quota columns.

Packed columns:

- `away_game_spread_spreadprice_moneyline__h1_spread_spreadprice_moneyline__q1_spread_spreadprice_moneyline`
- `home_game_spread_spreadprice_moneyline__h1_spread_spreadprice_moneyline__q1_spread_spreadprice_moneyline`
- `totals_game_total_overprice_underprice__h1_total_overprice_underprice__q1_total_overprice_underprice`

Encoding rules:

- `|` separates full game, first half, and first quarter.
- `,` separates the fields documented by the column name.
- `nodata` explicitly represents a missing value.

Example away value:

```text
3.5,-105,165|nodata,nodata,nodata|nodata,nodata,nodata
```

### `nfl_game_history` — completed regular-season results

One row per completed NFL regular-season game, initially backfilled for
2023–2025 from ESPN. This is separate from `nfl_games`, which remains the
current bot-facing upcoming-lines table.

The tab stores event/season/week identity, kickoff times, home and away teams
and final scores, home result/margin, total points, each team's conference and
division, `same_conference`, `same_division`, `matchup_type`,
`division_meeting_number`, neutral-site and overtime flags, generated tags, and
source provenance.

`matchup_type` is mutually exclusive:

- `division`
- `conference` (same conference, different divisions)
- `non_conference`

For divisional opponents, `division_meeting_number` is assigned
chronologically within the season and unordered team pair. Generated tags begin
with `divisional_game_1`, `divisional_game_2`, `conference_game`, or
`non_conference_game`, with `week_1`, `neutral_site`, and `overtime` appended
when applicable. The structured columns are authoritative; `tags` exists for
convenient Sheet filtering.

### `nfl_leans` — Telegram submissions

This is append-only with a finalized compact 33-column schema:

- Submission/user identity: deterministic `submission_id`, UTC/ET timestamps,
  Telegram user ID, username, first/last name, and message ID.
- Game identity: event/season/week metadata, kickoff times, teams, and bookmaker.
- Structured choice: period, market, and side.
- Selected market context: opening/latest selected line and price. Missing
  values use `nodata`.
- Full context: the same six packed opening/latest away, home, and totals columns
  used by `nfl_games`.
- Free text: `lean_text`.

The original 75-column provisional header expanded every period/field pair. It
was replaced before any submissions existed because the packed columns preserve
the same information with substantially less workbook cell growth.

`submission_id` is deterministically derived from Telegram user ID and message
ID. Before appending, the bot checks the first column and treats a repeated
message as already saved, preventing duplicate rows on retries.

### `suggestions` — freeform product feedback

Append one row for each reply to a `/suggest` ForceReply prompt:

- `submitted_at_utc`
- `submitted_at_et`
- `telegram_user_id`
- `telegram_username`
- `telegram_first_name`
- `telegram_last_name`
- `telegram_message_id`
- `suggestion`

The bot stores the prompt message ID in memory and accepts the suggestion only
when the incoming message replies to that exact prompt. Unrelated direct
messages are ignored. After a successful append, the pending prompt is cleared
and the persistent `/guess_nfl_game` and `/suggest` buttons are restored.

---

## Design decision — Google auth

There are currently **two different Google auth conventions** in the related
codebases:

- **A. line-movement pattern (adopted repo-wide):** base64-encoded
  `GOOGLE_CREDENTIALS` env var, scopes `spreadsheets` + `drive`, with
  `sheets_read`/`sheets_write` cooldown + 429-retry helpers
  (`line-movement/sheets_utils.py`).
- **B. forwarder pattern (to be retired):** `GOOGLE_SERVICE_ACCOUNT_JSON` (path to a
  service-account JSON file), scope `spreadsheets` only
  (`telegram-channel-forwarder/sheets.py`).

**Decision: A only.** Migrate the existing forwarder Sheets consumer and the new
intake service together so both decode `GOOGLE_CREDENTIALS` and share the proven
cooldown/backoff behavior. Remove `GOOGLE_SERVICE_ACCOUNT_JSON` after the
migration is deployed and verified; do not retain dual loaders or silent
fallbacks. The credential remains only in untracked environment configuration
and must never be committed.

---

## Conversation state

In-memory dict keyed by `telegram_user_id`:
`{ user_id: {"stage": ..., "sport": "nfl", "game": {...}, "lines": {...}, "market": ..., "side": ..., "prompt_msg_id": ...} }`.

- Set on game selection, advanced through period → market → side → text reply,
  and cleared on submission or when the user reopens the game browser.
- Guard: match the reply via `event.message.reply_to` pointing at the bot's
  ForceReply prompt (`prompt_msg_id`) so we don't capture unrelated DMs.

### Design decision — state storage

**Options considered:**
- **A. In-memory dict (recommended for v1, adopted).** Ephemeral per-process
  state.
- **B. SQLite table** (`intake_sessions`) in the existing DB that `listener.py`
  already uses (it hosts `reply_chains`).
- **C. Stateless** — encode the whole selected game + info into the callback data
  / prompt so no server state is needed.

**Decision: A for v1.** The flow is a few seconds long and a restart is
rare; if state is lost the user simply re-runs the command — cheap and obvious.
This is the least code. **Why not B (yet):** durability isn't worth a schema and
migration for a transient 3-step flow, but it's the clear upgrade if we later want
sessions to survive restarts (add an `intake_sessions` table next to
`reply_chains`). **Why not C:** Telegram callback data is capped at 64 bytes, too
small to carry a game blob + spread/total reliably, so we'd still need a lookup —
defeating the point. **Trade-off accepted:** in-flight forms are dropped on
restart.

For a future Telegram Web App, signed query parameters may carry a short-lived,
non-sensitive state identifier across restarts. The server must still validate
Telegram identity and load authoritative line snapshots server-side; raw prices
or trusted user data must not be accepted from the URL.

---

## Allowlist

Only an allowlist of Telegram user IDs may use the command (user-selected).

- Env var `INTAKE_ALLOWED_USER_IDS` — comma-separated numeric IDs in `.env`.
- Every handler (command, callback, reply) checks membership first; non-allowed
  users get a short "not authorized" reply and are ignored otherwise.

## Design decision — bot identity

**Options considered:**
Use a **dedicated intake bot** with its own token, session, name, and profile.
This is a resolved requirement, not an open decision. It prevents update-polling
conflicts, isolates failures and token rotation, and gives the intake experience
room for its own identity.

---

## Steps (each independently landable)

### Step 0 — Data-source setup (completed)
The dedicated workbook, service account, BetOnline Odds API source, tab schemas,
and environment variables are configured. Dedicated bot registration and the
allowlist remain intentionally deferred until the data pipeline is scheduled.

### Step 1 — `intake_sheets.py` (read + write, no bot)
- Port `get_gspread_client`, `sheets_read`, `sheets_write` (cooldown + 429) from
  `line-movement/sheets_utils.py`.
- `list_games(sport, date) -> list[dict]` — reads the game-list sheet
  (adapts `get_schedule_for_date`).
- `get_game_info(sport, game) -> dict` — reads opening/latest spread, moneyline,
  and total snapshots for the game.
- `append_submission(row: dict) -> None` — header-based append to intake sheet.
- **Verify:** a throwaway `python -c` / script call lists today's games and
  appends a test row locally (in a venv).

### Step 2 — `intake_bot.py` (bot skeleton + allowlist)
- Telethon `TelegramClient(StringSession(BOT_SESSION), API_ID, API_HASH)` started
  with `bot_token=BOT_TOKEN` (same as `listener.py`).
- `load_dotenv()` + `.env.local` override (repo convention).
- Register `events.NewMessage(pattern=r'^/guess_nfl_game')`, allowlist gate, reply
  with inline keyboard from `list_games`.
- **Verify:** `python intake_bot.py` locally; `/guess_nfl_game` returns the game
  list; non-allowlisted user is refused.

### Step 3 — Game, period, market, and side selection (completed)
- `events.CallbackQuery` handler (game): display opening/latest spread, total,
  and moneyline, then present full-game/first-half/first-quarter buttons.
- Period handler stores the period and presents
  `[ Spread ][ Moneyline ][ Total ]`.
- Market handler: store the selected market and present valid sides (teams for
  spread/moneyline, over/under for total).
- Side handler: store the side and send a `ForceReply` asking for the lean,
  rationale, and any line/price at which the preference changes.
- Callback data must be compact (Telegram 64-byte limit) — use short game
  index/key + type token into the state, not the full game blob.
- **Verify:** tapping Dolphins @ Bills shows correct opening/latest lines;
  selecting Spread → Miami Dolphins opens the expected reply prompt.

### Step 4 — Capture prediction + write row (completed)
- `events.NewMessage` (incoming, is-reply) handler: match `reply_to ==
  prompt_msg_id`, combine the stored line snapshot, market, side, and reply body,
  append the structured row, confirm to the user, and clear state.
- **Verify:** submitting writes a correct row to the intake sheet; confirmation
  echoes game · type · prediction.

Implementation verification completed with the finalized live worksheet schema
and 33 focused collector/bot tests. Live user testing successfully appended a
first-quarter total-under submission for a preseason game. Its selected
opening/latest fields correctly stored `nodata` because BetOnline had not
supplied Q1 markets, while all identity, game, selection, packed-context, and
lean fields were preserved. VPS deployment remains.

### Step 5 — Deploy artifacts (implemented)
- `run_intake_bot.sh` (mirror `run_grade_daemon.sh`).
- `deploy/systemd/telegram-intake.service` (mirror `grade-daemon.service`:
  `User=forwarder`, `EnvironmentFile=.env` + `-.env.local`, `Restart=on-failure`).
  No `WatchdogSec` needed for v1 (add later if it can wedge).
- Add the unit to `scripts/check_deploy_sync.sh` coverage.
- **Verify:** `bash scripts/check_deploy_sync.sh` clean; service starts on VPS,
  survives a restart, still handles `/guess_nfl_game`.

### Completed — 2026-08-04: VPS intake bot deployment

- Committed and pushed the native intake implementation.
- Merged only `INTAKE_BOT_TOKEN` and `INTAKE_ALLOWED_USER_IDS` into the existing
  VPS `.env`, preserving all unrelated values.
- Generated `INTAKE_BOT_SESSION` on the VPS as the `forwarder` user and retained
  `.env.local` ownership/mode requirements.
- Installed and enabled `telegram-intake.service`.
- Confirmed the service is active and running as `@nflguesser_bot`.
- Confirmed all repository deployment artifacts match their live VPS copies
  through `scripts/check_deploy_sync.sh`.

### Step 6 — Docs
- Add an "Intake bot" section to `CLAUDE.md` (service name, env vars, allowlist,
  sheet IDs, manual run command, test-mode notes).
- Update `requirements.txt` only if a new dep is truly needed (none expected).

---

## Extensibility (design for it, don't build yet)

- `/guess_nfl_game` is the first of a family (`/guess_nba_game`,
  `/guess_nhl_game`, …).
  Keep sport-specific config (source sheet id/tab, allowed prediction prefixes)
  in a small `INTAKE_SPORTS` dict/JSON in `.env` so new sports are config-only.
- Market and side are structured now. The initial version keeps rationale and
  movement-dependent conditions as free text; a later version may parse
  `target_line_or_price` automatically or collect it through another control.

---

## Testing / validation

- All Python work in a **venv** (never global).
- Local dry run: use a **test intake sheet** and the local bot session before
  touching the VPS. Follow the repo's cautious deploy rule — verify locally, then
  push + deploy only when confident (per `CLAUDE.md`).
- Watch for Telegram flood-waits on repeated bot restarts during dev.
- MOE and God Expert suites are Unix-only (`moe.py` imports `fcntl`): run
  them on the VPS from a scratch clone, never in `~/app`. Protocol and the
  twelve-module list: `docs/god-expert-roadmap.md`, Ground rules.

---

## Files changed / added

| File | Change | Notes |
|---|---|---|
| `nfl_lines.py` | New | BetOnline fetch, normalization, opening/latest merge, Sheet persistence |
| `scripts/fetch_nfl_lines.py` | New | Dry-run and `--write` CLI |
| `scripts/test_nfl_lines.py` | New | Parsing, season, opening, row-index, and snapshot tests |
| `run_nfl_lines_fetcher.sh` | New | Scheduled-mode VPS launcher |
| `deploy/systemd/nfl-lines-fetcher.service` | New | One-shot line fetch service |
| `deploy/systemd/nfl-lines-fetcher.timer` | New | 30-minute cadence-check timer |
| `intake_sheets.py` | New | Sheets read/write, ports line-movement helpers |
| `intake_bot.py` | New | Telethon bot: command, callback, reply, allowlist |
| `run_intake_bot.sh` | New | venv launcher |
| `deploy/systemd/telegram-intake.service` | New | systemd unit |
| `scripts/check_deploy_sync.sh` | Modified | include new unit |
| `CLAUDE.md` | Modified | document the intake bot |
| `.env` / `.env.local` | Modified | dedicated bot and intake env vars |
| `moe_god.py`, `moe.py`, `moe/experts.yaml` | Modified (2026-09-07) | veto/EV-floor knobs, prebuilt input, `claude_headless`, committee key, reason guard, disagreement report, mean-of-arms |
| `scripts/generate_moe_opinion.py`, `scripts/moe_grade.py` | Modified (2026-09-07) | `--input-file`, `--generation-backend`, `current_season_finals()`; paired report + mean-of-arms ledger rows |
| `scripts/god_judge_runner.py`, `run_god_judge.sh` | New (2026-09-07) | headless judge runner and its single-attempt wrapper |
| `deploy/systemd/god-judge.service` / `.timer` | New (2026-09-07) | :12/:42 timer, `TimeoutStartSec=3600`, loads `~/.claude/auth.env` |
| `scripts/fetch_nfl_lines_history.py` | New (2026-09-07) | nflverse closes + ESPN open/close pull, paced and resumable |
| `data/nfl_lines_history.csv`, `data/nfl_open_close.json` | New, committed (2026-09-07) | 2016–2025 closes (324 KB); 2024–2025 open/close (497 KB) |
| `scripts/test_god_judge_runner.py`, `scripts/test_nfl_lines_history.py`, `scripts/fixtures/god_week1/*`, `scripts/fixtures/nflverse_games_excerpt.csv`, `scripts/fixtures/espn_odds_*.json` | New (2026-09-07) | stubbed runner tests, offline parser tests, the persisted Week 1 rows |
| `.claude/skills/generate-nfl-moe-opinion/SKILL.md` | Modified (2026-09-07) | timer is the judge's normal path; input-file manual fallback |

New env vars:
- `INTAKE_ALLOWED_USER_IDS` — comma-separated numeric Telegram user IDs
- `NFL_INTAKE_SHEET_ID` — dedicated workbook containing all three intake tabs
- `INTAKE_BOT_TOKEN` / `INTAKE_BOT_SESSION` — dedicated intake bot credentials
- `GOOGLE_CREDENTIALS` — base64 service-account JSON, used repo-wide
- `ODDS_API_KEY` — The Odds API credential used for BetOnline lines
- `GOD_JUDGE_HEALTHCHECK_URL` (2026-09-07, optional, `.env`) — healthchecks.io
  ping for `run_god_judge.sh`; `ping_hc` no-ops unset
- `GOD_JUDGE_CLAUDE_BIN`, `GOD_JUDGE_CLAUDE_ISOLATION` (`safe-mode` default,
  `bare` never reads OAuth), `GOD_JUDGE_MAX_GAMES` (3), `GOD_JUDGE_RUNS_LOG`,
  `GOD_JUDGE_WORK_ROOT` (2026-09-07, all optional) — judge runner overrides.
  The runner's Claude token is `CLAUDE_CODE_OAUTH_TOKEN` from
  `~/.claude/auth.env`, loaded by the unit, not by an env file

---

## Resolved decisions

1. Start with a native Telegram prototype; consider a Web App only after using
   the prototype with realistic NFL data.
2. Run a separate process with a dedicated intake bot.
3. Keep transient multi-step state in memory for v1.
4. Use line-movement's base64 `GOOGLE_CREDENTIALS` approach repo-wide.
5. Require structured market and side selection; store the opening/latest line
   snapshot and keep reasoning/price conditions as text initially.
6. Use the Miami Dolphins in all sample user selections and submitted opinions.
7. Use BetOnline (`betonlineag`) through The Odds API as the fixed bookmaker so
   movement is always an apples-to-apples comparison.

## Implementation decision log

### 2026-08-04 — Dedicated workbook

**Decision:** Create a new workbook for this intake system rather than reuse any
`line-movement` workbook or tab.

**Rationale:** The projects may reuse code patterns, but their operational data,
permissions, and lifecycle should remain independent.

**Workbook layout:**

- `nfl_games` — one current row per Odds API event, optimized for bot reads.
- `nfl_line_snapshots` — append-only line history.
- `nfl_leans` — append-only Telegram submissions.
- `team_emojis` — editable team-name to emoji mapping used by bot views.
- `suggestions` — append-only freeform feedback with Telegram identity and
  submission timestamps.
- `allowed_users` — reference table containing each authorized user's current
  Telegram display name, numeric user ID, username when available, and optional
  comma-separated `moe_expert_ids`. Human MOE expert roles resolve from this
  table and fail closed on missing or duplicate assignments. Runtime intake
  enforcement remains the untracked `INTAKE_ALLOWED_USER_IDS` environment key.

There is no separate schedule-only tab. The Odds API event response supplies the
event ID, teams, and kickoff time together with the markets.

### 2026-08-04 — BetOnline as the fixed line source

**Decision:** Fetch `h2h`, `spreads`, and `totals` from BetOnline
(`betonlineag`) through The Odds API.

**Rationale:** Comparing the same bookmaker over time measures actual movement.
Selecting the best available book on each poll could create false movement when
the selected bookmaker changes.

Both active sport keys are collected:

- `americanfootball_nfl_preseason`
- `americanfootball_nfl`

### 2026-08-04 — Meaning of “opening”

**Decision:** “Opening” means the first valid value observed by this system for
each market, not necessarily BetOnline's true market-open price.

**Rationale:** The current Odds API poll supplies the latest price. If polling
begins after a market was posted, claiming that first captured value as the
book's original opener would be misleading. A market omitted in the first
response initializes its opening value when it first becomes available.

### 2026-08-04 — Current rows plus append-only history

**Decision:** Upsert every event into `nfl_games`, preserving opening fields and
updating latest fields. Append to `nfl_line_snapshots` only when any market
changes.

**Rationale:** The bot gets a small, fast current-state table while the snapshot
tab preserves movement history without adding unchanged hourly rows.

Manual blank rows in the workbook are tolerated: updates use physical Sheet row
numbers rather than positions in a filtered list.

### 2026-08-04 — Duplicate prevention

**Decision:** Every append path must perform an application-level duplicate
check before writing.

- `nfl_games` is unique by Odds API `event_id` and is updated in place.
- `nfl_line_snapshots` compares the candidate market payload with the latest
  persisted payload for that event and appends only when it differs.
- Multiple copies of an event returned or passed within one run are collapsed by
  `event_id`.
- `nfl_leans` will use a unique `submission_id`; retries must check that ID before
  appending.

A line returning to a previously seen value is **not** considered a duplicate
when an intervening value existed; that reversal is real movement and should be
recorded.

### 2026-08-04 — Full-game and period markets

**Decision:** Track spread, moneyline, and total for all three periods:

- Full game
- First half
- First quarter

Full-game markets come from the sport-level endpoint. First-half and first-quarter
markets require The Odds API's per-event endpoint, so the fetcher checks every
upcoming event on every run rather than limiting period checks to games within a
specific number of days.

**Rationale:** Period markets may be posted at different times, including well
before kickoff. Checking every event avoids missing their first observed value.
The live BetOnline probe returned no period markets for the currently available
games, but unavailable markets consumed no additional quota. Period fields remain
blank until BetOnline publishes them, then initialize their opening value from
the first valid observation.

The workbook schemas now include all three periods. `nfl_games` stores six packed
market columns (opening/latest × away/home/totals), while
`nfl_line_snapshots` stores three packed market columns (away/home/totals).
Missing values are written as the literal `nodata` so positions remain explicit
and survive CSV/Sheet transformations.

### 2026-08-04 — Google Sheets capacity

**Constraint:** Design against Google Sheets' standard **10 million cells per
spreadsheet** limit. The limit applies across every tab in the workbook, so
adding more tabs to the same spreadsheet does not increase capacity.

The packed format and change-only snapshot appends substantially reduce cell
growth, but they do not remove the long-term limit. Monitor workbook cell usage
before each season and establish an archival threshold well below 10 million.

When capacity becomes material, choose one of:

1. Archive completed seasons into additional **spreadsheet files** and keep the
   active season in the operational workbook.
2. Move line history to a database and retain Google Sheets only as a current
   view/export and human-facing intake surface.

A database is the preferred long-term destination if the history becomes large
or needs non-trivial querying.

### 2026-08-04 — Scheduling location

**Decision:** Run the recurring fetcher through a VPS systemd timer, not GitHub
Actions.

**Rationale:** Credentials already live on the VPS, systemd scheduling is more
predictable than scheduled Actions, and the repository already operates and
monitors recurring jobs this way.

The timer wakes every 30 minutes, but scheduled mode reads `nfl_games` first and
exits without an Odds API call when nothing is due. Adopted per-game bands:

| Time to kickoff | Poll interval |
|---|---:|
| More than 7 days | 24 hours |
| More than 24 hours through 7 days | 12 hours |
| More than 4 hours through 24 hours | 1 hour |
| 4 hours or less | 30 minutes |
| Started or past | Stop polling |

`last_updated_at` gates the sport-level full-game refresh.
`period_last_checked_at` independently gates first-half/first-quarter per-event
requests. Whenever a sport-level call is due, it returns all listed games, so
distant full-game lines may be refreshed more often at no additional request.
If a game's period request is not due, the writer preserves its last known H1/Q1
values rather than replacing them with `nodata` or recording false movement.

Implemented artifacts:

- `run_nfl_lines_fetcher.sh`
- `deploy/systemd/nfl-lines-fetcher.service`
- `deploy/systemd/nfl-lines-fetcher.timer`

---

## Remaining inputs

The data source, workbook, credentials, and schemas are resolved. Inputs still
needed before the Telegram bot is deployed:

1. **Allowlist** — the list of Telegram **user IDs** permitted to run
   `/guess_nfl_game`.
2. **Bot** — create the dedicated bot and provide its token/session plus desired
   display name and profile image.
3. **Slate scope** — decide whether `/guess_nfl_game` shows the current week, accepts
   a week/date argument, or offers both. Default recommendation: current week.
4. **Markets** — confirm the set is `Spread`, `Moneyline`, `Total`, `Other`, and
   define how `Other` should identify its market.
5. **Target condition structure** — confirm free text for v1, with an optional
   best-effort parsed `target_line_or_price`, rather than another required step.
