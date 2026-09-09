# Nightly ungraded-pick audit

`ungraded-audit.timer` → `ungraded-audit.service` → `run_ungraded_audit.sh` →
`scripts/ungraded_audit.py`, nightly at **04:05 ET**. Finds picks that never
got a verdict, decides (via a fresh headless `/investigate` agent per pick)
whether they *should* have graded, grades them and fixes the root cause in
code when yes, and DMs the operator a per-pick summary. Built 2026-09-07 on
the god-judge-runner pattern.

## What counts as a candidate

Scan of `parse_cache.json` (read-only — the runner never writes the cache):

- entry has `parsed.picks` and at least one leg whose `leg_verdicts` verdict
  is not WIN/LOSS/PUSH/VOID (mirror of the daemon's pending predicate; a
  lost parlay's pending siblings are moot, VOID counts as settled);
- its stale-reference date (mirror of `grade_daemon._stale_reference_date`:
  max of msg_date and every known `game_date`) is **yesterday or older** —
  today's games still belong to the daemon/tracker — and within
  `--days-back` (default 10; the pre-existing written-off backlog stays
  written off);
- `_failed` (daemon-retired) entries are prime candidates **except**
  `_failed_reason: "message deleted"` (nothing left to repair);
- `_dupe` entries are skipped (the primary is the candidate).

**Fan-out grouping**: copies of the same pick in multiple dest channels
(same capper + same unresolved descriptions + same reference date, cap 4)
form ONE group handled by ONE agent, which is told to fix every copy —
the multi-dest rule. Parking/attempts apply to every key in the group.

## The nightly run

Strictly sequential, `--max-picks` groups per night (default 3), flock'd
(`logs/.ungraded_audit.lock`) so two runners can never overlap; a 90-min
internal budget (`UNGRADED_AUDIT_BUDGET_MIN`) stops launching agents so the
run clears the 06:00 auto-reboot window. Each agent:

- fresh `claude -p "/investigate …"` in `/home/forwarder/app` — full tools,
  `--dangerously-skip-permissions`, model `UNGRADED_AUDIT_MODEL` (default
  claude-fable-5) at max effort, `--max-turns 150`, killed at
  `UNGRADED_AUDIT_AGENT_TIMEOUT` (default 1500 s; prompt says budget ~20 min);
- environment built from scratch: `CLAUDE_CODE_OAUTH_TOKEN` only — **no
  `ANTHROPIC_API_KEY`** (it's in `.env`; inheriting it would bill the API
  instead of the subscription; the scripts the agent runs `load_dotenv()`
  from disk themselves), plus `NIGHTLY_AUDIT=1`, which makes
  `telegram_resume_notify.py` and `post_investigate.sh` stand down (guards
  added to both hook scripts — keep them when editing the hooks);
- `--no-session-persistence`: nightly transcripts must not land in
  `~/.claude/projects/`, where the resume-notify hook would treat one as
  the channels session's "previous session". The stream-json file under
  `logs/ungraded_audit/<date>/` is the durable transcript instead;
- prompt constraints (see `build_prompt`): no worktree, no push, never
  touch telegram-forwarder, stop grade-daemon around any parse_cache edit,
  commit only its own files with a `nightly-audit:` message prefix, pinned
  tests + docs-first per CLAUDE.md, free sources only, lessons only for
  novel techniques, and it must end with one `AUDIT_RESULT: {...}` JSON
  line (`outcome` ∈ graded / fixed_needs_verify / legit_ungraded /
  needs_human / no_issue) that the runner parses for the DM.

After the loop, the **runner** (never the agents): one `git push` if HEAD
advanced, then `sudo -n systemctl restart grade-daemon` (persistent process
— old code otherwise; the tracker timer picks new code up by itself); if no
commits, it still ensures grade-daemon is active (an agent may have stopped
it and died). `listener.py` in the diff only adds a ⚠ DM note —
**telegram-forwarder is never auto-restarted** (flood-wait caution).
A failed/timeout/unparsed agent's uncommitted edits to tracked files are
reverted (only files it newly dirtied — pre-existing WIP is never touched);
untracked leftovers are reported, not deleted.

## Audit trail

- `logs/ungraded_audit_runs.jsonl` — one line per scan (`kind: "scan"`,
  every candidate group) and per agent call (`kind: "agent"`: keys, outcome,
  issue/action, commits + changed files, reverted paths, `total_cost_usd`,
  `usage`, `num_turns`, wall time, transcript path). This is the spend
  ledger for the nightly audit (subscription-billed, like
  `god_judge_runs.jsonl`; `claude_spend.jsonl` only sees API traffic).
- `logs/ungraded_audit/<date>/<key>.stream.jsonl` — the agent's full tool
  stream; `<key>.result.md` — its final report. Pruned after 90 days.
- `logs/ungraded_audit_state.json` — per cache key: attempts, last outcome,
  `parked`. Parked = never auto-retried: terminal outcomes
  (legit_ungraded / needs_human / no_issue) park immediately, anything else
  parks at `--attempt-cap` (default 2 nights). Every park is flagged in the
  DM. `git log --grep nightly-audit:` lists all agent commits.

## Operator interface

- DM via the watchdog bot, **Bot API HTML, one message per pick**
  (2026-09-09): a header (outcome tally + the runner's push/restart/⚠
  notes) followed by one **card** per audited group. Card = bold headline —
  outcome emoji + label (`OUTCOME_BADGE`), capper + description
  **hyperlinked to the pick's t.me message** (fan-out copies linked on a
  second line), ref date, `×N`, commit count, `[parked]` — with the agent's
  issue/action prose collapsed in a `<blockquote expandable>` (the
  desk-card "Why" pattern; the prompt asks for telegraph-style ≤90-char
  issue/action — hashes/dates/test names belong in the ledger). The static
  ledger/transcript paths are deliberately gone from the DM (they never
  change — see Audit trail above). A Telegram rejection of the HTML falls
  back to a tag-stripped plain send (`send_watchdog_dm(as_html=True)`), so
  a markup slip can't lose the report. **Silent when the scan found
  nothing** (watchdog convention) — the nightly `kind: "scan"` ledger line
  is still written, and `UNGRADED_AUDIT_HEALTHCHECK_URL` (unset today =
  silent no-op) is the liveness net.
- **Actionable cards** (2026-09-09): every card carries a 📋 follow-up
  button — a Bot API `copy_text` button (client-side, ≤256 chars) that
  copies an `inv …` prompt naming the pick, its cache key, outcome, and
  transcript path, ready to paste at the Claude session. Cards whose pick is
  still unresolved (every outcome except graded / no_issue) also get
  **✅ Win / ❌ Loss / 🟨 Push** buttons. Card facts persist in
  `logs/ungraded_audit_cards.json` (`register_cards`, 45-day retention —
  `callback_data` caps at 64 bytes, too small for the keys).
- **Verdict tap flow**: `claude-watchdog.service` (the interactive watchdog
  bot handles the callback; deploy = restart that service) runs
  `scripts/audit_mark.py <card_id> <verdict>`, which writes the verdict into
  every still-unresolved leg of every fan-out copy via the second-writer-safe
  cache API (`tracker_cache._load_pending_cache`/`_save_pending_cache` — no
  daemon stop), clears `_failed` so the pipeline picks the entry back up
  (grade-daemon emojis + broadcasts within ~10 s; the tracker's 5-min pass
  covers send_as_user channels), parks every key in the audit state (the
  audit never touches the pick again), and stamps the card `marked`. The bot
  then re-renders the card: status line appended, verdict buttons gone,
  follow-up kept. Settled legs are never overwritten; a second tap is a
  no-op (exit 3). Picks older than `MAX_AGE_DAYS` (12) are refused — a
  fully-resolved entry past `tracker_cache._EVICT_AFTER_DAYS` (14) would be
  evicted before broadcasting, silently losing the verdict; use the
  follow-up prompt for those. Manual CLI:
  `~/venv/bin/python scripts/audit_mark.py <card_id> WIN|LOSS|PUSH`.
  Test: `scripts/test_audit_mark.py`.
- `--dry-run` — plan + first prompt, calls nothing, writes only the scan
  ledger line. `--target <key>` (repeatable) — audit a specific entry now,
  bypassing scan/state gates. `--rearm <key>` — clear a parked key so the
  next night retries it. `--list-state`. `--no-dm`, `--no-push`.
- Kill switch: `UNGRADED_AUDIT_DISABLED=1` (via `scripts/set_env_local.py`)
  makes every run exit 0 immediately; or `sudo -n systemctl disable --now
  ungraded-audit.timer`.
- Env knobs: `UNGRADED_AUDIT_MAX_PICKS`, `_DAYS_BACK`, `_ATTEMPT_CAP`,
  `_AGENT_TIMEOUT`, `_BUDGET_MIN`, `_MAX_TURNS`, `_MODEL`, `_CLAUDE_BIN`,
  `_RUNS_LOG`, `_HEALTHCHECK_URL`, `_DISABLED`.

## Known limits / design choices

- Only cache-backed picks are audited. A pick the tracker never parsed at
  all (no `parse_cache` entry) is invisible to the scan — diffing
  `listener_forwarded` against the cache is a possible future extension.
- Agents share the repo working tree with the interactive session at 4 AM.
  They stage only their own files; the runner's revert logic only touches
  files an agent newly dirtied. Unrelated WIP (e.g. a modified file left
  overnight) survives untouched but WILL be racing agents if you're editing
  the same file at 4 AM — don't leave half-applied grading changes unpushed
  overnight if you can help it.
- Cost: up to 3 max-effort Fable agent runs/night on the subscription
  (shared rate budget with the interactive session + god judge — the reason
  for `--max-picks`, the attempt cap, and parking; measure via the ledger's
  `usage` fields). Zero marginal API dollars.
- Tests: `~/venv/bin/python -m unittest scripts.test_ungraded_audit_scan`
  (offline; pins scanner predicate, grouping, parking, AUDIT_RESULT
  parsing).
