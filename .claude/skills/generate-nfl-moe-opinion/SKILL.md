---
name: generate-nfl-moe-opinion
description: Generate an NFL MOE opinion with an allowed agent-session model, including Claude Code or GitHub Copilot, instead of the application's Anthropic API path.
---

# Generate an NFL MOE opinion with an agent

Use this skill when the user asks an agent to generate any registered NFL MOE
opinion without invoking the application's `ANTHROPIC_API_KEY` path. It is the
preferred interactive generation workflow for the Schedule, Divisional, Win
Total, and AK Experts, and the manual fallback for the God Expert judge
(`god_judge`), whose normal path is the headless timer described under
"God Expert". The active agent runtime has its own authentication, limits,
and billing.

## Invariants

- Use the exact prompt resolved for the selected model and deterministic input.
- Use the user-requested model only when it appears in the expert's
  `allowed_models`; otherwise use the expert's `default_model`.
- Perform inference at the selected model's configured reasoning effort,
  falling back to the expert-wide effort, and use long context when available.
- In Claude Code, use the current agent when it already matches the selected
  model and effort. Otherwise launch an isolated agent with those settings when
  the runtime supports per-agent model selection.
- In GitHub Copilot, launch a `general-purpose` subagent with the selected model,
  configured reasoning effort, and long context when that model is available.
  If it is not available in Copilot, use a matching Claude Code session.
- Do not let the agent fetch outside information, inspect unrelated files, or
  change the supplied input.
- Persist every raw response through `scripts/generate_moe_opinion.py`; never
  write directly to the Sheet.
- Never approve an opinion automatically. Review the persisted opinion with the
  user under the normal hash-bound approval workflow.
- Store temporary inputs and responses outside the repository and remove them
  after persistence.

## Procedure

1. Read `moe/experts.yaml`, resolve any exact-model entry under
   `model_prompts`, then read that prompt and the relevant sections of
   `docs/telegram-intake-plan.md`. The selected prompt path, prompt version,
   output schema, and prompt hash must be the values persisted for the run.
2. Produce the exact deterministic input without inference:

   ```bash
   python scripts/generate_moe_opinion.py \
     --event-id <event-id> --expert <schedule|divisional|win_total|ak> \
     --show-input > <temporary-input.json>
   ```

3. Run one isolated agent inference:
   - Model: requested allowed model, otherwise the expert default
   - Reasoning effort: selected model override, otherwise expert-wide value
   - Context: long context when available

   Give it the complete registered expert prompt and exact contents of the
   temporary input. Instruct it to perform only that expert inference and
   return exactly one raw JSON object matching the prompt, with no Markdown
   fence or surrounding explanation.

4. Save the agent's exact response as `<temporary-opinion.json>`. Do not correct
   its claims before persistence; validator failures are audit records.

5. Validate the deterministic response structure before any optional
   factuality inference. If validation fails, retain the invalid audit row and
   allow at most one fresh targeted repair using the exact error and original
   response. Never retry until a response happens to pass.

6. For an output-schema-v4 expert whose deterministic structure passed, run a
   second isolated inference with the
   same selected model and configured effort. Give it:
   - the complete registered factuality prompt;
   - the same exact deterministic input;
   - the exact `nondeterministic_analysis` claims from the first response.

   Save its exact raw JSON as `<temporary-factuality.json>`.

7. Persist through the normal pipeline. Only output-schema-v4 responses use a
   separate factuality response; path-only schema v7 does not:

   ```bash
   # Schedule, Win Total, or AK Expert
   python scripts/generate_moe_opinion.py \
     --event-id <event-id> --expert <schedule|win_total|ak> \
     --model <selected-model> \
     --generation-effort <actual-agent-effort> \
     --agent-response <temporary-opinion.json>

   # Divisional Expert using output schema v4
   python scripts/generate_moe_opinion.py \
     --event-id <event-id> --expert divisional \
     --model <selected-model> \
     --generation-effort <actual-agent-effort> \
     --agent-response <temporary-opinion.json> \
     --agent-factuality-response <temporary-factuality.json>

   # Divisional Expert using path-only output schema v7
   python scripts/generate_moe_opinion.py \
     --event-id <event-id> --expert divisional \
     --model <selected-model> \
     --generation-effort <actual-agent-effort> \
     --agent-response <temporary-opinion.json>
   ```

8. Confirm the persisted row records:
   - `model=<selected-model>`
   - `generation_backend=agent_runtime`
   - `generation_effort=<actual-agent-effort>`
   - `review_status=pending`

9. Review factual accuracy and policy compliance one section at a time. Approve
   only the exact persisted opinion using `scripts/review_moe_opinion.py`.

## God Expert

- `god_rules` never uses an agent. `python scripts/generate_moe_opinion.py
  --event-id <event-id> --expert god_rules --deterministic` computes and
  persists the rules opinion from the approved committee rows and the
  BetOnline market.
- `god_judge` accepts exactly one model, `claude-fable-5-1`, and two
  backends: `claude_headless` (the timer) and `agent_runtime` (the manual
  fallback below); `--api` is refused. Its `--show-input` output is the
  masked judge request: voices labeled `Voice A…` in a seeded shuffle, lenses
  described without names. Give the agent exactly that document plus
  `moe/prompts/god_judge/v1.md`. Never tell it which expert or person a voice
  belongs to, and never hand it the full aggregator input or the sheet.
- The judge returns only probabilities and reasons; the application derives
  the side and total legs. Every `W-L` record and "N games" count a reason
  cites must appear in the request, or follow from it (a voice's projected
  score, the winner-vote split, the cohort size a record implies); an
  invented number fails validation and persists as an audit row.
- Run the judge after every voice for the game is approved. It reads only
  approved, hash-verified rows, one per expert, on that expert's registry
  default model.
- **The judge never runs in a session that has seen unmasked committee
  rows.** One fresh session per judge run, the request in, the response out.

### Normal path: the timer

`god-judge.timer` runs `scripts/god_judge_runner.py` every 30 minutes at :12
and :42. For each upcoming game with a complete committee (an approved row
for every enabled non-aggregator expert) it builds one input, persists the
rules arm on it, runs one headless `claude -p` call (Fable 5.1 at max
effort, every tool disabled, the registered prompt as the whole system
prompt, from an empty directory whose environment holds no sheet
credentials), persists the judge row with backend `claude_headless`, and
DMs the reviewer through the watchdog bot. It dedupes on the committee key
(voice opinion ids plus the latest lines and prices), stops two hours before
kickoff, gives up on a committee after two invalid judge rows, and never
approves anything. `python scripts/god_judge_runner.py --dry-run` prints the
plan without persisting or calling anything.

### Manual fallback: a fresh interactive session

Use this only when the timer cannot run, from a session that has never
printed unmasked committee rows. The input file pins both arms to one sheet
state, so the judge no longer races the 30-minute lines fetcher:

1. `python scripts/generate_moe_opinion.py --event-id <id> --expert
   god_rules --show-input > input.json` and note the `input_sha256` printed
   on stderr (the full aggregator input's hash).
2. `python scripts/generate_moe_opinion.py --event-id <id> --expert
   god_rules --deterministic --input-file input.json` persists the rules row
   on exactly that input.
3. `python scripts/generate_moe_opinion.py --event-id <id> --expert
   god_judge --show-input --input-file input.json > request.json` prints the
   masked request derived from the file; note its `input_sha256` (the
   request's hash, which is what the judge row persists). Nothing is re-read
   from the sheet's opinions, snapshots, or finals.
4. Run one isolated Fable 5.1 inference at max effort with
   `moe/prompts/god_judge/v1.md` as the whole prompt and `request.json` as
   the only input, and save its exact raw JSON as `response.json`.
5. `python scripts/generate_moe_opinion.py --event-id <id> --expert
   god_judge --agent-response response.json --input-file input.json
   --expected-input-sha256 <request sha> --model claude-fable-5-1
   --generation-effort <effort>` persists the judge row; the backend is
   `agent_runtime` by default (`--generation-backend claude_headless` only
   for a response captured from a headless `claude -p` call).
6. Review each row with `python scripts/review_moe_opinion.py --opinion-id
   <id> --status approved --reviewed-by <you>`.

### Rating voice

`rating_elo` never uses an agent either: it is Elo arithmetic on the
committed prior `moe/priors/nfl_elo_v1.json` and this season's finals
(`moe_rating.py`, spec `moe/prompts/rating_elo/v1.md`). One game:
`python scripts/generate_moe_opinion.py --event-id <id> --expert rating_elo
--deterministic` (`--show-input` prints the rating input). The weekly path
is `python scripts/generate_rating_week.py --season <S> --week <N>` (one
pending row per upcoming game of the week, deduped on the input hash), then
`python scripts/review_moe_opinion.py --expert rating_elo --week <N>
--reviewed-by <you>` to read the week's table and the same command with
`--approve` to approve it. The judge runner needs the approved rating row
before it judges a game. Refit the prior each offseason with
`python scripts/fit_nfl_elo.py --check-season <season just played>`.

## Runtime notes

### Claude Code CLI

Start Claude Code with the selected allowed model and configured effort, or
select those settings before invoking the skill. The current Claude agent may
perform the isolated inference itself, provided it uses only the registered
prompt and generated input and writes the exact raw JSON to the temporary path.
Direct application API generation requires the explicit `--api` fallback flag.

### GitHub Copilot CLI

Use the task/subagent facility with a `general-purpose` agent, the selected
allowed model, configured reasoning effort, and long context. Instruct the
agent to write its exact raw JSON to the temporary path. If Copilot does not
offer the selected model, use a matching Claude Code session instead.
