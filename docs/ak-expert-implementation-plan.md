# AK Expert Implementation Plan

## Purpose

Add an **AK Expert** to the NFL mixture-of-experts system in
`telegram-channel-forwarder`.

AK is an authorized Telegram intake user who records NFL picks, projected
scores, and reasoning. The expert should learn how AK's projections have
historically differed from the betting market and use those calibration
patterns to recommend the best full-game side and full-game total for a
specific NFL game.

The expert is not intended to replace AK's judgment or mechanically fade every
disagreement with the market. Its purpose is to answer questions such as:

- When AK projects more scoring than the market, how often does the game
  actually finish under?
- When AK projects a favorite to win by more or fewer points than the spread,
  does that tendency differ for home and road teams?
- When AK and the market disagree about which team should be favored, which
  side has historically performed better?
- Is a projected scoring difference concentrated on one team's implied total?
- Did similar disagreements perform differently at the line available when AK
  submitted the pick versus the closing line?
- Does the current pick resemble a recurring calibration strength, weakness,
  or no-signal sample?

The resulting opinion should appear beside the existing Schedule Expert in the
Telegram MOE views. It should provide both:

1. A full-game side recommendation or `PASS`.
2. A full-game total recommendation or `PASS`.

The implementation must preserve the existing system's auditability,
append-only storage, exact-input persistence, human approval, source hashing,
and fail-closed Telegram display behavior.

## Implementation status

Implemented locally:

- AK-only exact-score capture and normalized score columns.
- Conservative historical score parsing plus a report-first backfill command.
- Deterministic submission/current/closing market comparison and historical
  side/total grading.
- Independent NFL calibration samples for submission and closing lines.
- A versioned WNBA cold-start prior with separate side/total decay.
- Positive NFL total gaps map explicitly to reviewed WNBA ranges:
  0–<3 → 0–<6, 3–<9 → 6–<12, 9–<12 → 12–<16, and 12+ → 16+.
- Output schema v5 with independently selectable spread and total opinions.
- Opus 4.8 with maximum reasoning through either the Anthropic API or the
  shared agent-runtime skill.
- Append-only persistence, approval-hash compatibility, and AK-specific
  Telegram summary, model-picker, and detail rendering.

Still required before live use:

- Append the four `nfl_leans` and three `moe_opinions` columns with the guarded
  migration.
- Configure `AK_TELEGRAM_USER_ID` in untracked local and VPS `.env.local`
  files.
- Review the historical parse report before applying any backfill.
- Generate, review, and explicitly approve the first Rams-49ers opinion.


## Product background

### Existing NFL intake

The native Telegram intake flow records picks in the append-only `nfl_leans`
worksheet. Each row contains:

- Telegram identity and a deterministic submission ID.
- NFL event, season, week, kickoff, and team identity.
- Selected period, market, and side.
- BetOnline opening and latest line context at submission time.
- Packed opening and latest snapshots for both teams and totals.
- AK's free-text `lean_text`.

The intake is already structured enough to know the game and selected betting
market. It does not currently normalize an exact projected away and home score.
Those scores are required to measure overestimation and underestimation of:

- The expected winning margin.
- The full-game total.
- Each team's market-implied team total.

### Existing NFL history

The `nfl_game_history` worksheet contains completed regular-season NFL games
with:

- Final away and home scores.
- Home result and margin.
- Total points.
- Home/away team identity.
- Conference, division, and matchup type.
- Week, date, neutral-site, overtime, and generated tags.

This supplies deterministic outcomes for grading historical AK projections.

### Existing market history

`nfl_line_snapshots` is append-only and preserves observed BetOnline movement.
It can identify:

- The market available when AK submitted a pick.
- The last valid observed market before kickoff.
- Whether a move held, reverted, crossed pick'em, or remained stable.

The implementation must distinguish the **submission market** from the
**closing market**. A line saved in `nfl_leans` is the latest line observed at
submission time and must not be mislabeled as the close.


## Existing MOE architecture

The Schedule and Divisional experts established the NFL mixture-of-experts
architecture. AK Expert extends the same generation, persistence, review, and
display system rather than introducing a separate path.

### Versioned expert registry

`moe/experts.yaml` defines:

- Stable expert ID and display name.
- Expert version.
- Prompt version and immutable prompt path.
- Input profile.
- Output schema version.
- Default and allowed models.
- Enabled state and initial weight.

### Whitelisted input builder

`build_schedule_input()` in `moe.py` constructs the only data the Schedule
Expert is allowed to see. It does not pass a raw Sheet row to the model.

The builder:

- Selects allowed fields.
- Produces deterministic historical summaries.
- Omits unavailable fields rather than guessing.
- Recursively rejects known prohibited market and live-state fields.
- Serializes the exact model input for persistence and hashing.

AK Expert needs the same whitelist-first approach, but its contract
intentionally includes AK picks and BetOnline market data.

### Strict output validation

The existing validator checks:

- Exact team names.
- Score and winner consistency.
- Probability and expected-margin consistency.
- Numeric ranges and finite values.
- Required evidence and counter-evidence.
- Maximum thesis and Sheet-cell lengths.
- Unsupported week claims when week metadata is absent.

AK Expert needs an expert-specific schema because it returns two independently
graded betting recommendations rather than only a straight-up winner.

### Append-only persistence and approval

Every generation attempt is written to `moe_opinions`:

- Valid output starts as `review_status=pending`.
- Invalid output is also persisted with the exact raw response and error.
- Sheet append failures are spooled locally for later replay.
- Human approval binds to a SHA-256 of the exact opinion, event, input, prompt,
  model, source, and output.
- Telegram only shows valid, approved, untampered output.

AK Expert must retain all of these guarantees.

### Manual generation

Generation is deliberately separate from Telegram navigation:

```bash
python scripts/generate_moe_opinion.py \
  --event-id <event_id> --expert schedule --show-input

python scripts/generate_moe_opinion.py \
  --event-id <event_id> --expert schedule
```

The bot only reads approved persisted opinions. It does not invoke a model
during button interaction. AK Expert should begin with the same manual
workflow.


## Agreed product decisions

### Prediction capture

Use existing AK `nfl_leans` history when an explicit projected score can be
parsed safely.

For future AK submissions, require an exact projected away and home score. Do
not require scores from other intake users.

### Recommendation scope

Always evaluate and return both:

- A full-game side.
- A full-game total.

Either leg may be `PASS`.

### WNBA cold-start prior

Use the reviewed WNBA calibration tendencies as a small, explicitly labeled,
strictly capped cross-sport prior.

NFL observations must supersede the WNBA prior as AK's NFL history grows.
The source's `+6` WNBA threshold was explicitly provisional. The reviewed
mapping now uses explicit NFL and WNBA absolute bands rather than copying one
threshold or scaling solely by percentage.


## Definitions

### Predicted margin

The absolute difference between AK's projected away and home scores.

### Predicted total

The sum of AK's projected away and home scores.

### Submission market

The valid BetOnline line captured in the `nfl_leans` row when AK submitted the
pick.

### Closing market

The last valid BetOnline observation in `nfl_line_snapshots` before kickoff.

### Market-implied team scores

For a spread with absolute value `S` and total `T`:

```text
favorite implied score = (T + S) / 2
underdog implied score = (T - S) / 2
```

### Side margin gap

When AK and the market have the same favorite:

```text
side margin gap = AK predicted winning margin - market spread magnitude
```

- Positive: AK estimates a larger win than the market.
- Negative: AK estimates a smaller win than the market.

### Favorite flip

AK and the market make opposite teams the favorite.

### Total gap

```text
total gap = AK predicted total - market total
```

- Positive: AK estimates more scoring than the market.
- Negative: AK estimates less scoring than the market.

### Team-total gap

```text
team-total gap = AK predicted team score - market-implied team score
```


## Proposed future AK intake behavior

### AK identity

Configure AK through an untracked environment value:

```text
AK_TELEGRAM_USER_ID=<numeric Telegram user ID>
```

Do not hardcode a personal Telegram identifier in source control.

### Required score format

The free-text lean prompt shown to AK should request an exact score in a
canonical, team-labeled format:

```text
Score: Dallas Cowboys 24, Philadelphia Eagles 27
Rationale: Philadelphia has the stronger matchup, but the spread may be high.
```

The parser may support a limited set of equivalent explicit formats, but it
must always prove:

- Which score belongs to the away team.
- Which score belongs to the home team.
- That exactly one score prediction is present.
- That both scores are non-negative integers.
- That the projected game is not tied.

If the score is missing or ambiguous, reject the AK submission with an example
of the required format. Do not save a partially normalized AK prediction.

Other authorized intake users continue using the existing flow without a
required projected score.

### Proposed `nfl_leans` columns

Append these columns to the existing schema:

```text
predicted_away_score
predicted_home_score
prediction_parse_version
prediction_parse_status
```

Expected future AK rows:

```text
prediction_parse_status=parsed
```

Expected non-AK rows:

```text
predicted_away_score=
predicted_home_score=
prediction_parse_version=
prediction_parse_status=not_applicable
```

Historical rows may use:

```text
parsed
missing
ambiguous
conflicting
```


## Historical AK score backfill

Create a deterministic parser for existing AK `lean_text`.

### Backfill rules

1. Filter rows by `AK_TELEGRAM_USER_ID`.
2. Match the row's exact away and home team names and known abbreviations.
3. Extract only explicit integer score pairs.
4. Require a provable team-to-score mapping.
5. Reject tied predictions.
6. Reject multiple incompatible score predictions.
7. Never use an LLM to silently choose between ambiguous mappings.

### Backfill statuses

`parsed`
: One unambiguous projected away/home score was found.

`missing`
: No exact score was supplied.

`ambiguous`
: A score pair exists, but team ordering cannot be proven.

`conflicting`
: The text contains multiple incompatible score predictions.

### Review-first workflow

The backfill command should initially run in report-only mode and produce:

- Submission ID.
- Event ID and matchup.
- Original `lean_text`.
- Parsed away and home score, if any.
- Parse status.
- Explicit reason for exclusion.

Only reviewed `parsed` rows should be written back.

The write path must:

- Update only the four new normalization columns.
- Preserve all original fields.
- Be idempotent.
- Refuse to overwrite a nonblank normalized score unless an explicit repair
  mode and guarded expected values are supplied.


## AK calibration data construction

Add a whitelist builder such as:

```python
build_ak_input(
    game,
    history,
    leans,
    line_snapshots,
    *,
    ak_user_id,
    wnba_prior,
)
```

Do not pass raw worksheets directly to the model.

### Current-game input

Include:

- Event, season, week, kickoff, away team, and home team.
- Matchup type.
- AK's current structured pick rows for this event.
- AK's current exact predicted score.
- AK's current rationale.
- BetOnline opening, submission-time, and latest available full-game spread,
  moneyline, and total.
- Deterministically calculated market-implied team scores.
- Net movement and movement quality.

Do not include:

- Current or live game scores.
- Unrelated users' picks.
- Raw credentials or environment values.
- Unsupported injury, roster, weather, or news claims.
- Historical outcomes that occurred after the prediction being evaluated.

### Historical prediction eligibility

A historical AK prediction enters calibration only when:

- The normalized score status is `parsed`.
- The NFL game has a matching final result in `nfl_game_history`.
- The prediction was submitted before kickoff.
- The submission market is valid for the relevant calculation.
- The closing snapshot can be selected deterministically when closing
  calibration is requested.

Rows that fail eligibility must be reported under excluded-history counts with
specific reasons.

### Preventing temporal leakage

For each historical prediction:

- Use only market observations captured at or before the relevant timestamp.
- Select the close as the final valid snapshot before kickoff.
- Use the final result only for grading after the game.
- Do not expose future snapshots or later revisions as if AK had seen them.

For the current game:

- Include no completed-game result.
- Include only snapshots captured before generation.


## Deterministic historical grading

For every eligible historical AK prediction, calculate:

### Straight-up outcome

Whether AK's projected winner won the game.

### Spread outcome at submission

Whether AK's projected side covered the spread available when the pick was
submitted.

### Spread outcome at close

Whether AK's projected side covered the closing spread.

### Total outcome at submission

Whether the actual total finished over, under, or pushed the total available
when AK submitted.

### Total outcome at close

Whether the actual total finished over, under, or pushed the closing total.

### Team-total outcomes

For each team:

- Actual score versus submission-market implied score.
- Actual score versus closing-market implied score.
- AK prediction error.
- Absolute AK prediction error.

### Calibration categories

- Same favorite or favorite flip.
- AK laid more, near market, or laid less.
- Positive, neutral, or negative total gap.
- Which team accounts for the majority of the total disagreement.
- Home/road role of AK's projected side.
- Home/road role of the market favorite.
- Favorite/underdog role.
- Spread-size band.
- Matchup type.
- Whether side and total movement held, reverted, or remained stable.


## Initial calibration buckets

### Total-gap buckets

```text
<= -13
-12 to -6
-5 to -1
near zero
+1 to +5
+6 to +12
>= +13
```

### Side-gap buckets

```text
favorite flip
laid >3 points less
laid 1-3 points less
within 1 point
laid 1-3 points more
laid >3 points more
```

### Required bucket output

Every supplied bucket should contain:

- Wins, losses, and pushes.
- Sample size.
- Chronological outcome string.
- Submission-line record.
- Closing-line record.
- Home/road split.
- Favorite/underdog split.
- Supporting submission IDs.

Do not let the model derive new numeric cross-splits from raw history. If a
cross-split is potentially useful, calculate it deterministically and provide
it as an explicit input field.


## WNBA cold-start prior

The WNBA prior comes from `apps/wnba-poller/path210.md` and the reviewed
`wnba_tendencies.txt` analysis in the `www` repository. The useful transferable
findings are:

### Total overestimation

When the projected total materially exceeded the market:

- Original 37-game study, gap at least +6: under 8/9.
- Original extreme band, gap at least +12: under 5/5.
- Fresh standardized sample, gap at least +6: under 6/8.

The repeated direction is that a large positive projection-market total gap is
more useful as an under warning than an over endorsement.

The decision-facing mappings are:

- NFL 0–<3 → WNBA 0–<6: under 3/4 in the available fresh sample.
- NFL 3–<9 → WNBA 6–<12: under 3/4 earlier and 4/6 fresh, or 7/10 combined.
- NFL 9–<12 → WNBA 12–<16: under 2/2 fresh; the earlier 12+ sample cannot yet
  be separated from 16+.
- NFL 12+ → WNBA 16+: no isolated reviewed record is available.

The cumulative original and fresh counts remain stored for auditability but
must not be presented as a narrower band than the source supports.

### Total underestimation

The result is less stable:

- Original gap at most -6: under 7/10.
- Fresh gap at most -6: under 2/4.

A large negative gap must not be mechanically converted into an over or under.
It needs NFL evidence and another supporting signal.

### Venue interaction

In the fresh standardized WNBA sample:

- Positive total gap with a road projected side: under 6/7.
- Positive total gap with a home projected side: under 3/5.
- Negative total gap with a home projected side: under 5/6.
- Negative total gap with a road projected side: under 1/5.

These samples are small and may reflect team selection rather than a durable
venue effect. They are candidate interactions only.

### Side-margin interaction

In the fresh same-favorite sample:

- AK laid more than the market with a road favorite: covered 5/6.
- AK laid more than the market with a home favorite: covered 2/5.
- AK stayed within one point of market: covered 4/6.
- AK laid materially less than market: covered 2/4.

The result argues against a monotonic "larger predicted margin means greater
confidence" rule. Venue and favorite role matter.

### Favorite flips

Two recent favorite/dog flips both failed to cover for AK's projected side.
Earlier WNBA history contains counterexamples, so this is a warning rather than
an automatic market-side bet.

### Team scores

Recent same-favorite projections were approximately centered near the market
on average but had about 11.5 points of mean absolute error per team. Large
single-team divergences should be treated as uncertainty and game-script
warnings.


## Versioned WNBA prior file

Add:

```text
moe/priors/ak_wnba_v1.json
```

The file should contain structured records, sample sizes, definitions, source
version, and applicability notes. The prompt should not scrape or interpret a
prose file at runtime.

Example shape:

```json
{
  "schema_version": 1,
  "source": {
    "repository": "www",
    "path": "wnba_tendencies.txt",
    "path210_through_entry": 163
  },
  "max_equivalent_nfl_observations": 2,
  "expires_at_nfl_bucket_size": 8,
  "max_confidence_star_adjustment": 1,
  "total_gap": {
    "positive_6_or_more": {
      "original_under": 8,
      "original_games": 9,
      "fresh_under": 6,
      "fresh_games": 8
    }
  }
}
```


## Hard WNBA-prior limits

The WNBA prior:

- Must always be labeled `cross_sport_prior`.
- Must never be represented as NFL evidence.
- Has a maximum weight of two equivalent NFL observations per matching bucket.
- Cannot create a pick when AK has no current prediction.
- Cannot increase side or total confidence by more than one star.
- Cannot override a sufficiently populated contradictory NFL sample.
- Has zero weight once the matching NFL bucket reaches eight resolved
  predictions.
- Cannot be the sole reason for a recommendation stronger than one star.

These rules should be enforced deterministically in the input builder, not left
only as prompt instructions.


## Expert registration

The implemented registry entry is:

```yaml
ak:
  name: AK Expert
  mode: agent
  version: 1
  prompt_version: 1
  prompt: prompts/ak/v1.md
  input_profile: ak_calibration
  output_schema_version: 5
  default_model: claude-opus-4-8
  allowed_models:
    - claude-opus-4-8
  enabled: true
  initial_weight: 1.0
```

Generation uses maximum reasoning effort. The expert can run through the
application's Anthropic transport or through the shared agent-runtime skill;
both paths retain the same validation, persistence, and approval gate.


## AK output schema v5

```json
{
  "predicted_winner": "Philadelphia Eagles",
  "predicted_away_score": 20,
  "predicted_home_score": 24,
  "home_win_probability": 0.60,
  "expected_home_margin": 3.5,
  "side": {
    "market": "spread",
    "selection": "Philadelphia Eagles",
    "line": -2.5,
    "confidence_stars": 2,
    "evidence": [
      "exact catalog id"
    ],
    "counterarguments": [
      "exact catalog id"
    ]
  },
  "total": {
    "selection": "Under",
    "line": 47.5,
    "confidence_stars": 2,
    "evidence": [
      "exact catalog id"
    ],
    "counterarguments": [
      "exact catalog id"
    ]
  },
  "discarded_considerations": [
    "Potential factor discarded because required data was unavailable."
  ]
}
```

The model returns `evidence_ids` and `counterargument_ids`; application code
resolves those IDs into deterministic factual cards and renders the thesis,
calibration summary, no-signal factors, and complete opinion. Generic
winner/score/probability fields remain validated and stored for compatibility.

### Side pass

```json
{
  "market": "pass",
  "selection": "PASS",
  "line": null,
  "confidence_stars": 1,
  "evidence": [],
  "counterarguments": [
    "Why no side edge is actionable."
  ]
}
```

### Total pass

```json
{
  "selection": "PASS",
  "line": null,
  "confidence_stars": 1,
  "evidence": [],
  "counterarguments": [
    "Why no total edge is actionable."
  ]
}
```


## AK prompt requirements

Add the immutable initial prompt:

```text
moe/prompts/ak/v1.md
```

It must require:

- Use only supplied JSON.
- Return exactly one JSON object.
- Independently evaluate side and total.
- Permit `PASS` on either leg.
- Cite exact bucket records and sample sizes.
- Distinguish submission-line and closing-line history.
- Distinguish NFL observations from the WNBA cross-sport prior.
- Never inflate a WNBA sample into an NFL record.
- Give small samples little weight.
- Never infer injuries, weather, roster state, current form, or news unless
  those fields are explicitly supplied by a future input version.
- Never assume a market disagreement proves AK or the market is correct.
- Explain which team contributes most to a total gap.
- Explain favorite flips explicitly.
- Separate measured evidence, counterarguments, no-signal factors, and
  discarded considerations.
- Keep winner, exact score, probability, and expected margin internally
  consistent.
- Never recommend a stale line that is not present in the current input.

Required `full_opinion` layout:

```text
AK projection
Market gaps
Side pick
Why the side
Why the side may be wrong
Total pick
Why the total
Why the total may be wrong
NFL calibration
WNBA cold-start prior
No signal
Discarded considerations
Conclusion
```


## `moe.py` changes

### Input-profile dispatch

Current generation rejects every input profile except `schedule_only`.

Replace the single hardcoded path with explicit dispatch:

```python
if expert["input_profile"] == "schedule_only":
    input_payload = build_schedule_input(game, history)
elif expert["input_profile"] == "ak_calibration":
    input_payload = build_ak_input(
        game,
        history,
        leans,
        line_snapshots,
        ak_user_id=ak_user_id,
        wnba_prior=wnba_prior,
    )
else:
    raise NotImplementedError(...)
```

Do not add a generic raw-input fallback.

### Generation arguments

Extend `generate_opinion()` with optional explicit dependencies:

```python
leans: list[dict[str, Any]] | None = None
line_snapshots: list[dict[str, Any]] | None = None
ak_user_id: str | None = None
```

Schedule generation must continue to work without these arguments.

AK generation must fail clearly if any required dependency is missing.

### Expert-specific validation

Preserve the existing schedule validator behavior.

Add AK validation for:

- Exact team names.
- Exact current side and total lines.
- Side selection valid for the selected side market.
- Total selection exactly `Over`, `Under`, or `PASS`.
- `PASS` line must be null.
- Non-pass line must equal a line supplied in the input.
- Side and total stars are integers from 1 through 5.
- Side and total evidence/counterargument lists have the required shape.
- Predicted winner, scores, home probability, and expected margin agree.
- Calibration statements do not cite nonexistent bucket identifiers.
- WNBA prior statements are labeled as cross-sport.
- Required output remains below Sheet and Telegram limits.

### Source hash

For AK Expert, `_source_sha256()` must include:

- `moe.py`
- AI transport.
- NFL line and history helpers.
- Generation CLI.
- `moe/experts.yaml`
- AK prompt.
- Versioned WNBA prior.
- Any dedicated AK parsing or calibration module.


## Persistence changes

Append these fields to `OPINION_HEADERS`:

```text
side_pick_json
total_pick_json
calibration_summary_json
```

Store normalized JSON for both recommendations and the calibration summary.

The existing generic fields remain populated:

- `predicted_winner`
- `predicted_away_score`
- `predicted_home_score`
- `home_win_probability`
- `expected_home_margin`
- `thesis`
- `full_opinion`

For AK Expert:

```text
pick_market=side_and_total
pick_side=<compact side and total summary>
```

The structured JSON columns remain authoritative.


## Approval-hash compatibility

Appending fields to `moe_opinions` must not invalidate existing approved
Schedule Expert opinions.

Make `opinion_output_sha256()` output-schema-aware:

- Output schema v2 uses the existing hash payload unchanged.
- Output schema v5 includes:
  - `side_pick_json`
  - `total_pick_json`
  - `calibration_summary_json`

Add regression tests proving an existing approved schema-v2 Schedule Expert row
retains the same hash after AK Expert support is added.


## Generation CLI changes

`scripts/generate_moe_opinion.py` currently loads `nfl_games` and
`nfl_game_history`.

For `ak_calibration`, also load:

- `nfl_leans`
- `nfl_line_snapshots`

Pass `AK_TELEGRAM_USER_ID` explicitly into the builder.

Manual input inspection:

```bash
python scripts/generate_moe_opinion.py \
  --event-id <event_id> --expert ak --show-input
```

Manual generation:

```bash
python scripts/generate_moe_opinion.py \
  --event-id <event_id> --expert ak
```

Every model call must continue to persist its complete attempt. Do not add an
unpersisted inference-preview mode.


## Telegram presentation

The MOE overview should show:

```text
AK Expert
Side: Eagles -2.5 ★★
Total: Under 47.5 ★★
Score: Cowboys 20, Eagles 24
```

The detail view should display the required prompt sections and paginate using
the existing MOE detail machinery.

The footer should continue showing:

- Expert version.
- Exact model ID.
- Prompt hash.

Pending, rejected, invalid, stale, reassigned, or hash-mismatched AK opinions
must remain hidden.


## Recommended implementation structure

Keep `moe.py` from becoming a collection of unrelated parsing rules.

If the implementation is more than a small addition, introduce:

```text
moe_ak.py
```

Suggested responsibilities:

- AK score parsing.
- Historical prediction eligibility.
- Submission and closing market selection.
- Deterministic grading.
- Calibration bucket aggregation.
- WNBA prior loading and cap enforcement.
- AK input construction.
- AK output validation helpers.

`moe.py` should retain:

- Registry loading.
- Generic generation orchestration.
- Persistence.
- Approval hashing.
- Shared Telegram rendering.
- Explicit input-profile dispatch.

Avoid a speculative framework. Extract only the AK-specific behavior needed to
keep the generic MOE orchestration readable.


## Tests

### Score parsing

- Parses the canonical team-labeled format.
- Parses approved equivalent explicit formats.
- Rejects missing scores.
- Rejects unlabeled ambiguous score pairs.
- Rejects conflicting multiple scores.
- Rejects negative, decimal, or tied scores.
- Does not confuse betting lines or odds with final scores.
- Maps scores to away/home correctly regardless of which team AK names first.

### Intake behavior

- AK submission without an exact score is rejected.
- AK submission with a valid score writes normalized fields.
- Non-AK submission remains unchanged and does not require a score.
- Duplicate submission behavior remains idempotent.
- Existing `nfl_leans` columns and packed market context remain intact.

### Historical eligibility

- Includes only AK rows.
- Includes only predictions submitted before kickoff.
- Includes only games with final history.
- Excludes malformed historical scores with explicit counts.
- Prevents future or current-game result leakage.

### Market selection

- Uses the stored submission market for submission comparisons.
- Selects the last valid pre-kickoff snapshot as the close.
- Rejects post-kickoff snapshots.
- Handles missing markets explicitly.
- Detects held, reverted, stable, and favorite-flip movement.

### Grading

- Grades favorite and underdog spread covers.
- Grades spread pushes.
- Grades total over, under, and push.
- Grades team totals against market-implied scores.
- Handles home and away projected sides.
- Handles favorite flips.

### Calibration

- Assigns exact total-gap buckets at boundaries.
- Assigns exact side-gap buckets at boundaries.
- Separates submission and closing records.
- Produces correct home/road and favorite/dog splits.
- Preserves chronological outcome order.
- Includes supporting submission IDs.
- Does not count excluded history.

### WNBA prior

- Never contributes more than two equivalent observations.
- Cannot create a pick without an AK projection.
- Cannot increase confidence by more than one star.
- Has zero weight at eight NFL observations.
- Is explicitly labeled cross-sport.
- Cannot override a sufficiently populated contradictory NFL bucket.

### Output validation

- Accepts valid side and total recommendations.
- Accepts independent passes.
- Rejects unknown teams.
- Rejects unavailable lines.
- Rejects invalid total directions.
- Rejects inconsistent winner, score, probability, or margin.
- Rejects malformed evidence lists.
- Rejects invented calibration bucket IDs.

### Persistence and display

- Every valid and invalid attempt is persisted.
- Existing schema-v2 approval hashes remain unchanged.
- Schema-v3 hashes include AK structured fields.
- Tampered approved AK opinions are hidden.
- Overview displays both recommendations.
- Long detail is paginated.
- Telegram callback data remains below 64 bytes.


## Documentation changes

Update `docs/telegram-intake-plan.md` with:

- AK Expert product purpose.
- Prediction normalization schema.
- Historical backfill procedure.
- AK input contract.
- WNBA prior source and cap.
- Output schema v5.
- Manual generation and review commands.
- Sheet migration and rollback steps.
- Deployment and operational verification.

Document every prompt revision after live review, following the existing
Schedule Expert decision-log pattern.


## Rollout sequence

### 1. Apply guarded worksheet migrations

Append the new columns to:

- `nfl_leans`
- `moe_opinions`

Migration must validate the existing prefix exactly before appending columns.
Do not reorder or silently replace headers.

### 2. Implement and test the historical parser

Run report-only mode against AK history.

Review:

- Parsed rows.
- Ambiguous rows.
- Conflicting rows.
- Coverage percentage.

Do not apply the backfill until the report is accepted.

### 3. Apply the approved backfill

Write only reviewed `parsed` rows and their normalized score/version/status
columns. Ambiguous, conflicting, and missing rows remain untouched.

Verify idempotency by rerunning the backfill.

### 4. Inspect AK input without inference

```bash
python scripts/generate_moe_opinion.py \
  --event-id <event_id> --expert ak --show-input
```

Manually verify:

- Current AK prediction.
- Current market.
- Submission and closing distinctions.
- Historical bucket arithmetic.
- Supporting submission IDs.
- Excluded-history reasons.
- WNBA prior cap.

### 5. Generate one pending opinion

Run AK Expert manually for one game.

Review the exact persisted:

- Input JSON.
- Raw response.
- Structured side.
- Structured total.
- Calibration citations.
- Source and prompt hashes.

### 6. Approve one opinion

Use the existing review command:

```bash
python scripts/review_moe_opinion.py \
  --opinion-id <uuid> \
  --status approved \
  --reviewed-by <reviewer> \
  --note "<review basis>"
```

### 7. Verify Telegram

Confirm:

- AK Expert appears beside Schedule Expert.
- Both side and total are visible.
- Detail pagination works.
- Pending or rejected revisions remain hidden.
- Existing Schedule Expert opinions remain visible and hash-valid.

### 8. Keep generation manual

Do not generate during Telegram interaction.

Review multiple real outputs and revise the prompt/input contract before
considering scheduled or automatic generation.


## Failure handling

- Missing AK identity: fail before loading history.
- Missing current AK projection: return an explicit no-current-prediction error;
  do not fabricate an opinion.
- Missing exact score: reject future AK intake or exclude historical row.
- Missing closing line: retain submission calibration and mark closing evidence
  unavailable.
- No eligible history: generate only if a current AK projection exists, label
  NFL calibration as no signal, and use only the capped WNBA prior.
- Invalid model output: persist exact raw output and error, then raise.
- Sheet append failure: use the existing MOE spool.
- Schema mismatch: fail migration or startup visibly; do not silently reshape
  rows.


## Non-goals for the first version

- Automatic Telegram-time inference.
- Automatic betting or external posting.
- Injury, roster, weather, or news analysis.
- Using other users' picks as AK history.
- A global ensemble weighting AK Expert against Schedule Expert.
- Retrofitting inferred scores into ambiguous historical prose.
- Treating WNBA rates as NFL observations.
- Optimizing stake size.
- First-half, first-quarter, prop, or derivative-market recommendations.


## Acceptance criteria

AK Expert is ready for initial manual use when:

1. Future AK submissions require and persist an exact projected score.
2. Existing unambiguous AK scores can be backfilled through a reviewed,
   idempotent process.
3. AK input contains no other users' picks and no temporal leakage.
4. Submission and closing markets are labeled separately.
5. Historical grading and calibration are deterministic and tested.
6. The WNBA prior is versioned, labeled, capped, and expires by NFL bucket.
7. The expert returns independently validated side and total recommendations
   or passes.
8. Every generation attempt is append-only and auditable.
9. Only exact human-approved, untampered output is visible in Telegram.
10. Existing Schedule Expert generation, approval hashes, and Telegram views
    remain compatible.
