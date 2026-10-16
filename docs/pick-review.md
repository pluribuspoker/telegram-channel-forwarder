# Pick review — Claude reviews every pick (2026-10-15)

Operator ask (2026-10-15): "audit every single pick, both for odds and failing to parse and failing to grade … at time of send and overnight … have the reviewer spawn an agent if needed and report to me what its outcome was. No restrictions on what the agent can do." No shadow mode.

The older loops only bring in Claude when a heuristic fires: the odds gate's free checks, grader anomalies, the ungraded-audit scan. Pick review asks Claude about **every** pick, then hands what it flags to an agent.

## Review pass — `pick-review.timer` (every 3 min) → `scripts/pick_review.py`

The pass sends ONE subscription call per review question (`pick_reviewer.review`, headless `claude -p` via `claude_sub`). It runs Opus 5.5 at `medium`, with no tools and never `ANTHROPIC_API_KEY`. The tool-less call can't fetch anything, so the payload carries the evidence: the post text with our tags stripped and blockquotes marked, every parsed field, the bound game and kickoff time, the price and its source, and the verdict with its `calc`.

| stage | question | asked when |
|---|---|---|
| `parse` | Does the parse match the post? Covers missing or invented picks, sport/team/market/period/line/direction, parlay vs straight, wrong bound game, price for a different bet | each source message (fan-out copies = one question), `msg_date` today/yesterday ET |
| `grade` | Is the verdict right? `espn` = the bound event's line/box score from ESPN (free, `fetch_espn_summary` / soccer summary) | each newly resolved leg, messages ≤ 3 days old |
| `noparse` | The parser found no picks. Was there a bet? | `picks.db` grades rows `calc in ('parse failed','no picks extracted')`, last 36 h, identical texts = one question |
| `nightly` | Yesterday end to end, including legs still unresolved after their game | from 05:45 ET, batches of 5 source messages |

Each question is keyed by stage + source (+ leg) + a **signature** of what was reviewed (the parsed picks; verdict + calc). An unchanged question is never asked twice. A changed one is asked again, so an agent's fix gets re-reviewed. Every review is logged to `logs/pick_review.jsonl`. A failed or unparseable review is retried on the next pass and dropped after 3 failures on the same signature. `PICK_REVIEW_MAX_CALLS` (40) caps the calls per pass; the rest wait for the next pass.

**The prompts' conventions are the false-positive filter.** At ship time the first full sweep (47 questions over 3 days) raised 7 flags, and every one came from a convention the reviewer didn't know:

- Totals settle mid-game by design.
- A pick's null `sport` inherits the post's sport.
- A moneyline's `line` sometimes holds the price; grading ignores it.

Once these were in `_COMMON`, a re-run raised 0 of those flags. The same prompts caught all 8 replayed historical bug classes: game-title total split into a parlay, a card of straights as a parlay, "Washington today" bound to Sunday, LSU→Detroit Tigers, teaser legs at stated+7, a wrong spread verdict, a soccer draw graded PUSH, and "Kings NBA Pre Season" never parsed.

**Tuning rule:** when an agent reports `false_alarm` for a class, add the convention to `_COMMON` and re-run the flagged questions (`--rearm` + `--dry-run --call`). Never loosen the reviewer by telling it to flag less in general.

A reviewer only **flags**. It never changes a parse or verdict itself: sampled answers aren't deterministic.

## Agent pass — `pick-review-agent.service` (`--agent`)

Flags queue in `logs/pick_review_state.json` `pending`, one item per source message. A second flag merges into a message that is already pending. Then the review pass starts the agent unit.

The agent pass runs ONE headless `/investigate` agent (Opus 5.5 high, subscription, the `HeadlessInvoker` chassis shared with odds-watch and hc-repair) over up to 4 messages. Its outcomes:

- `fixed`: repaired, and the class fixed in code
- `repaired`: live artifacts repaired, no code change needed
- `false_alarm`
- `already_handled`: odds-watch, the ungraded audit or hc-repair got there first
- `needs_human`

**Full authority** (operator): the agent edits, commits (`pick-review:`), pushes (after `run_tests.py`) and restarts services. The one exception is `claude-channels`, the operator's live chat. telegram-forwarder restarts are allowed only for a `listener.py` change, at most once, because of Telegram flood waits.

The runner backstops the agent:

- pushes any unpushed commits
- restarts grade-daemon on a `.py` change
- starts grade-daemon again if the agent left it stopped
- reverts a failed agent's uncommitted edits

**Every outcome is DM'd** as a card via the watchdog bot, false alarms included. Transcripts go to `logs/pick_review/<stamp>/` (prompt.md, agent.jsonl, result.md) and the ledger to `logs/pick_review_runs.jsonl`.

## Guards

- `PICK_REVIEW_DISABLED=1`: nothing runs.
- `PICK_REVIEW_AGENT_DISABLED=1`: flags are DM'd raw instead of going to an agent.
- One agent at a time (flock `logs/.pick_review_agent.lock`). It stands down while the nightly audit, odds watch or hc-repair holds its lock (1 GB box). The next 3-minute pass restarts it.
- A message flagged again after `PICK_REVIEW_ATTEMPT_CAP` (2) agent runs per stage is DM'd raw ("🚩 flagged, no agent").
- `PICK_REVIEW_DAILY_CAP` (12) agents per rolling 24 h is a runaway breaker. At the cap, pending flags are DM'd raw once.

## Cost (estimate 2026-10-15)

Each review call is about $0.01–0.04 notional and takes 3–8 s. A typical day is about 10–25 source messages, which is about 40–80 calls, or about $15–25 a week notional. Agent runs cost about $1–7 each, but run only on flags. Usage is shared with the chat session, so watch `logs/claude_sub_calls.jsonl` (source `pick_review`) and the agent ledger.

## Manual

```bash
python scripts/pick_review.py --dry-run            # list pending questions + payloads
python scripts/pick_review.py --dry-run --call     # also ask Claude; writes nothing
python scripts/pick_review.py --nightly-now        # yesterday's batch now
python scripts/pick_review.py --agent --dry-run    # print the agent prompt for pending flags
python scripts/pick_review.py --rearm <key>        # forget reviews/attempts matching key
```

Test: `scripts/test_pick_review.py` (offline).
