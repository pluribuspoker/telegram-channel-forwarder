---
name: generate-nfl-moe-opinion
description: Generate an NFL MOE opinion with a Claude Opus 4.8 Copilot subagent instead of the Anthropic API.
---

# Generate an NFL MOE opinion with a subagent

Use this skill when the user asks an agent to generate a Schedule or Divisional
Expert opinion without charging the repository's `ANTHROPIC_API_KEY`.

## Invariants

- Use the exact registered expert prompt and deterministic input.
- Launch a `general-purpose` subagent with model `claude-opus-4.8`, reasoning
  effort `max`, and long context when available.
- Do not let the subagent fetch outside information, inspect unrelated files,
  or change the supplied input.
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
     --event-id <event-id> --expert <schedule|divisional> \
     --show-input > <temporary-input.json>
   ```

3. Launch one `general-purpose` subagent with:
   - `model: claude-opus-4.8`
   - `reasoning_effort: max`
   - `context_tier: long_context`

   Give it the complete registered expert prompt and exact contents of the
   temporary input. Instruct it to perform only that expert inference and
   return exactly one raw JSON object matching the prompt, with no Markdown
   fence or surrounding explanation.

4. Save the subagent's exact response as `<temporary-opinion.json>`. Do not
   correct its claims before persistence; validator failures are audit records.

5. For an output-schema-v4 expert, launch a second Opus 4.8 maximum-reasoning
   subagent. Give it:
   - the complete registered factuality prompt;
   - the same exact deterministic input;
   - the exact `nondeterministic_analysis` claims from the first response.

   Save its exact raw JSON as `<temporary-factuality.json>`.

6. Persist through the normal pipeline:

   ```bash
   # Schedule Expert
   python scripts/generate_moe_opinion.py \
     --event-id <event-id> --expert schedule \
     --model claude-opus-4-8 \
     --agent-response <temporary-opinion.json>

   # Divisional Expert
   python scripts/generate_moe_opinion.py \
     --event-id <event-id> --expert divisional \
     --model claude-opus-4-8 \
     --agent-response <temporary-opinion.json> \
     --agent-factuality-response <temporary-factuality.json>
   ```

7. Confirm the persisted row records:
   - `model=claude-opus-4-8`
   - `generation_backend=copilot_subagent`
   - `generation_effort=max`
   - `review_status=pending`

8. Review factual accuracy and policy compliance one section at a time. Approve
   only the exact persisted opinion using `scripts/review_moe_opinion.py`.
