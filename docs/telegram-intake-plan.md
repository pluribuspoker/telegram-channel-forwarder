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
- The same main inference may return freeform `nondeterministic_analysis`
  claims. A separate versioned factuality prompt classifies each exact claim as
  supported, reasonable inference, or unsupported against the same controlled
  input. Only supported claims and `Interpretation:`-prefixed reasonable
  inferences are appended to Telegram; unsupported claims remain audit-only.
  If the checker labels a claim usable but its cited paths fail deterministic
  validation, code downgrades that claim to unsupported rather than invalidating
  the deterministic opinion. Numeric discarded text is likewise excluded from
  rendered output while remaining preserved in the raw response.
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

Structurally valid output begins with `review_status=pending`. It is excluded
from bot views until a human reviews the exact persisted text and marks it
`approved`; rejected output remains preserved but hidden. Review metadata
includes UTC timestamp, reviewer, note, and the hash of the exact approved
output, event, expert, prompt, model, source, and input identity. Bot reads
recompute that hash and hide any row changed or reassigned after approval.
Manual review:

```bash
python scripts/review_moe_opinion.py \
  --opinion-id <uuid> --status approved \
  --reviewed-by <reviewer> --note "<why it is safe to expose>"
```

The NFL game detail view now links to a paginated MOE summary. The summary shows
the latest valid, approved persisted opinion for each expert. If an expert has
approved opinions from multiple models, selecting that expert opens a model
picker containing the latest approved run per exact model ID; a single-model
expert opens directly. Expert detail is paginated to stay below Telegram's
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

# Generate and persist an opinion:
python scripts/generate_moe_opinion.py \
  --event-id <nfl_games event_id> --expert schedule

# Compare the same expert using another model:
python scripts/generate_moe_opinion.py \
  --event-id <nfl_games event_id> --expert schedule \
  --model claude-opus-4-8
```

There is deliberately no inference-only preview flag: every model call persists
its complete input and output. `--show-input` is safe because it does not invoke
the model.

Both enabled experts use `claude-opus-4-8` with Anthropic
`output_config.effort=max`. The Divisional Expert's separate factuality request
uses the same model and effort. New rows persist `generation_backend` and
`generation_effort`; approval hashes bind both values when present while legacy
approved rows without them retain their existing hashes.

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

- Future AK submissions require a canonical away/home score and persist four
  append-only normalization fields in `nfl_leans`. Other intake users are
  unchanged.
- `moe_ak.py` builds a whitelisted schema-v5 input from AK's latest projection,
  the submission and current BetOnline markets, the last matching pre-kickoff
  snapshot, and completed historical outcomes. Reviewed normalized scores are
  authoritative; legacy prose is parsed conservatively and conflicting,
  ambiguous, tied, or missing projections are excluded.
- Side and total history are independently eligible. Missing spread data does
  not erase total evidence, and missing total data does not erase side
  evidence. Submission-line and closing-line records remain separate.
- `moe/priors/ak_wnba_v1.json` provides the reviewed WNBA cold-start prior.
  Side and total weights decay independently, are capped at two equivalent NFL
  observations, and expire at eight matching resolved NFL predictions. The
  source's provisional WNBA `±6` threshold is not copied as six NFL points:
  positive NFL gaps map as 0–<3 → WNBA 0–<6, 3–<9 → WNBA 6–<12,
  9–<12 → WNBA 12–<16, and 12+ → WNBA 16+. The supported records are
  Under 3/4 for the first band, 7/10 for the second, and 2/2 fresh for the
  third; the last band has no isolated reviewed sample.
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

`emergency_migration.txt` now documents the implementation requirements,
lossless export/import format, one-day service freeze and SQLite cutover,
verification gates, backups, and rollback with post-cutover delta replay. It
must be reconciled with the final script names after the backend implementation
and rehearsed against a production export before use.

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

New env vars:
- `INTAKE_ALLOWED_USER_IDS` — comma-separated numeric Telegram user IDs
- `NFL_INTAKE_SHEET_ID` — dedicated workbook containing all three intake tabs
- `INTAKE_BOT_TOKEN` / `INTAKE_BOT_SESSION` — dedicated intake bot credentials
- `GOOGLE_CREDENTIALS` — base64 service-account JSON, used repo-wide
- `ODDS_API_KEY` — The Odds API credential used for BetOnline lines

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
  Telegram display name, numeric user ID, and username when available. Runtime
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
