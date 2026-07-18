---
description: Debug and investigate issues with live data, logs, and Telegram messages
argument-hint: <issue description, Telegram link, or error message>
---

# Investigate

$ARGUMENTS

## Rules

1. **Detect your environment first.** Check if you're running locally (Windows) or on VPS (Linux, hostname `pickbot`). Use `uname -s` or check if `/home/forwarder/app` exists. This determines whether you SSH or run commands directly.
2. **Live data is on the VPS.** If local, SSH to VPS. If on VPS, run commands directly as forwarder.
3. **Telegram messages are accessible.** Use `scripts/vps_msg.py` on VPS. If local, run via SSH. If on VPS, run directly.
4. **Don't give up after one failed attempt.** Try variations before concluding data doesn't exist.
5. **Use a git worktree for code changes** (see CLAUDE.md worktree pattern). Do ALL commits in the worktree before merging.

## Workflow

1. **Detect environment**: Check if running on VPS or locally
2. **Gather context**: Fetch the Telegram message if linked, check parse_cache and/or DB
3. **Read the relevant code** before theorizing
4. **Verify VPS matches local** (if local): `ssh root@209.38.51.86 'cd /home/forwarder/app && git log --oneline -1'`
5. **Check VPS logs** (`journalctl -u telegram-tracker`, `journalctl -u grade-daemon`, etc.)
6. **Identify root cause**: Trace the full pipeline before fixing
7. **Fix and verify** with the real data that triggered the bug (replay actual inputs through the fixed code)
8. **Deploy code fix first** (push + deploy) before touching live data
9. **Fix live data** if needed (wrong emoji, DB entry, etc.). Use Bot API with `parse_mode: "HTML"` — check `msg.media`: use `editMessageCaption` for photo/video, `editMessageText` for plain text
10. **Run tracker manually** scoped to the affected channel for instant verification

## VPS queries

**If running on VPS** (e.g., via Telegram channels):
```bash
cd ~/app && ~/venv/bin/python scripts/vps_msg.py <channel_id> <msg_id>
cd ~/app && ~/venv/bin/python scripts/vps_grades.py --msg-id <id>
cd ~/app && ~/venv/bin/python scripts/vps_grades.py --search <term>
cd ~/app && ~/venv/bin/python tracker.py --live --channel <channel_id>
```

**If running locally** (SSH into VPS):
```bash
ssh root@209.38.51.86 'su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/vps_msg.py <channel_id> <msg_id>"'
ssh root@209.38.51.86 'su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/vps_grades.py --search <term>"'
ssh root@209.38.51.86 'su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --channel <channel_id>"'
```

## Lessons

1. Never inline Python in SSH commands beyond simple one-liners. Write a temp script, `scp` it, run it.
2. For grading bugs, trace the ENTIRE pipeline (`claude_parse` → post-parse → `validate_sport` → `build_context` → fetcher → `claude_grade`) before fixing.
3. Always use `--channel <id>` when running `tracker.py --live` locally — unscoped runs broadcast to production.
4. **Log queries: mind the timezone.** `journalctl --since/--until` uses the VPS **local time (America/New_York, EDT)**, but `graded_at` (DB/cache) and Telegram `ts` are **UTC**. Subtract 4h (UTC→EDT) or the query misses entries.
5. **`systemctl is-active` lies about hangs.** Verify liveness by the recency of its last log line, not `is-active` alone.
6. **`vps_msg.py`/Telethon need the `-100`-prefixed channel ID.** A `t.me/c/<short>/<msg>` link gives the short ID; prepend `-100` or calls fail.
7. **"Didn't run" usually means "ran and crashed."** For a scheduled job (cron/timer) that "sometimes doesn't fire," read its output log (`/tmp/*_cron.log`, `journalctl -u <unit>`) for a traceback BEFORE suspecting the scheduler — it usually ran fine and died late in the pipeline. The fix is resilience (retry + explicit timeouts), not the schedule.
8. **"Parsed as UNKNOWN" is usually a coverage gap, not a parse failure.** When a pick parses correctly (right sport/teams/bet_type) but grades UNKNOWN, suspect the score fetcher's competition list, not the parser — UNKNOWN comes from `build_context` returning `CONTEXT_SKIP` when the fetcher finds no game, often because the league isn't in its list (e.g. `SOCCER_LEAGUES`). Verify by hitting the ESPN scoreboard for the right league code (`soccer/swe.1`, …) directly; if ESPN has the game, add the code to the fetcher's list.
9. **"Nothing happened" may just be "not processed yet."** Before diagnosing why odds/grading didn't happen for a pick, compare the message post time (Telegram `ts`, UTC) against the last `telegram-tracker` cycle (`systemctl list-timers`, EDT — subtract 4h). The tracker runs every 5 min; a pick posted after the last run simply hasn't been scanned yet, and no cache entry will exist. A missing cache entry + a message newer than the last cycle = wait for (or manually trigger) the next run, not a bug. `tracker.py --live --channel <id>` processes it immediately.
10. **A "sent twice" duplicate is often a source-side delete-and-repost, not a listener race.** The listener dedups on `(channel, dest, msg_id)`. When a capper deletes and re-posts (or double-taps send), the repost gets a **new msg_id**, so both copies pass the id guards and forward. Ground truth is the `listener_forwarded` table (`sqlite3 picks.db`): a duplicate shows as two adjacent source ids with the same content. Confirm by fetching each id directly (`get_messages(src, ids=<id>)`, NOT topic-filtered — a deleted/reposted id can be invisible to a topic-scoped fetch); the earlier usually returns `<none>` (deleted at source). Fix is content-dedup, not id-dedup.
11. **`sport=Other` + `odds_match_type=sport_unsupported(Other)` = an entirely unsupported sport, not a mis-parse.** Distinct from lesson #8 (sport supported, league/competition missing): here the sport isn't in the parse enum at all, so Claude falls back to "Other", which skips odds (`sport_unsupported`) and grading (`build_context` → `CONTEXT_SKIP` → UNKNOWN). Adding a new sport is a 3-line pattern: enum in `_PARSE_PROMPT` (`ai.py`) + `ESPN_LEAGUES` (`scores.py`) + `SPORT_KEYS` (`odds.py`); check The Odds API `/v4/sports/?all=true` and the ESPN `site.api...` scoreboard for the right keys first (e.g. PLL lacrosse = `lacrosse_pll` / `lacrosse/pll`). Add a post-parse safety net + prompt team roster like the WNBA/CFL blocks so nicknames resolve to canonical full names.
12. **A code fix to the parser does NOT retroactively re-parse already-cached picks.** The tracker reuses `cached["parsed"]` and skips `claude_parse` on subsequent runs (to avoid re-paying), so after you fix a classification/team/bet_type bug and deploy, the offending pick STILL shows the old wrong parse. You must invalidate its `parse_cache.json` entry to force a fresh parse. For a **non-forwarded-only** channel, delete the whole `<channel>:<msg>` entry (tracker re-parses from scratch on the next run). For a **forwarded-only** channel, do NOT delete it — the tracker's gate requires `"parsed" in entry` (or `_forwarded`), so a deleted entry gets skipped; instead edit the parse in place and drop `odds_by_pick` to re-fetch. Check membership with the `grade_forwarded_only` flag in `MAPPINGS_CONFIG`. Then run `tracker.py --live --channel <id>` to verify. (Restart `grade-daemon` first if the fix touched grading code — it's a persistent process and won't see the new code until restarted, and stopping it during the cache edit also avoids a re-grade race.)
After resolving, review whether this investigation revealed any **generalizable** lessons — operational traps, debugging principles, or constraints that apply broadly across unrelated future investigations. If yes, add them above. If the fix was code-only (a bug you fixed in the source), or the mistake was too incident-specific to recur, **skip this step** — no lesson needed. Do not add a lesson just to have one.
