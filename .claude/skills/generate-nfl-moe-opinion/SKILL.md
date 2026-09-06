---
name: generate-nfl-moe-opinion
description: Generate an NFL MOE opinion with an allowed agent-session model, including Claude Code or GitHub Copilot, instead of the application's Anthropic API path.
---

# Generate an NFL MOE opinion with an agent

Use this skill when the user asks an agent to generate any registered NFL MOE
opinion without invoking the application's `ANTHROPIC_API_KEY` path. It is the
preferred interactive generation workflow for the Schedule, Divisional, Win
Total, and AK Experts. The active agent runtime has its own authentication,
limits, and billing.

## Invariants

- Use the exact registered expert prompt and deterministic input.
- Use the user-requested model only when it appears in the expert's
  `allowed_models`; otherwise use the expert's `default_model`.
- Perform inference at the expert's configured reasoning effort and with long
  context when available.
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

1. Read `moe/experts.yaml`, the registered expert prompt, and the relevant
   sections of `docs/telegram-intake-plan.md`.
2. Produce the exact deterministic input without inference:

   ```bash
   python scripts/generate_moe_opinion.py \
     --event-id <event-id> --expert <schedule|divisional|win_total|ak> \
     --show-input > <temporary-input.json>
   ```

3. Run one isolated agent inference:
   - Model: requested allowed model, otherwise the expert default
   - Reasoning effort: the expert's configured value
   - Context: long context when available

   Give it the complete registered expert prompt and exact contents of the
   temporary input. Instruct it to perform only that expert inference and
   return exactly one raw JSON object matching the prompt, with no Markdown
   fence or surrounding explanation.

4. Save the agent's exact response as `<temporary-opinion.json>`. Do not correct
   its claims before persistence; validator failures are audit records.

5. For an output-schema-v4 expert, run a second isolated inference with the
   same selected model and configured effort. Give it:
   - the complete registered factuality prompt;
   - the same exact deterministic input;
   - the exact `nondeterministic_analysis` claims from the first response.

   Save its exact raw JSON as `<temporary-factuality.json>`.

6. Persist through the normal pipeline:

   ```bash
   # Schedule, Win Total, or AK Expert
   python scripts/generate_moe_opinion.py \
     --event-id <event-id> --expert <schedule|win_total|ak> \
     --model <selected-model> \
     --agent-response <temporary-opinion.json>

   # Divisional Expert
   python scripts/generate_moe_opinion.py \
     --event-id <event-id> --expert divisional \
     --model <selected-model> \
     --agent-response <temporary-opinion.json> \
     --agent-factuality-response <temporary-factuality.json>
   ```

7. Confirm the persisted row records:
   - `model=<selected-model>`
   - `generation_backend=agent_runtime`
   - `generation_effort=max`
   - `review_status=pending`

8. Review factual accuracy and policy compliance one section at a time. Approve
   only the exact persisted opinion using `scripts/review_moe_opinion.py`.

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
