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

- Never inline Python in SSH commands beyond simple one-liners. Write a temp script, `scp` it, run it.
- For grading bugs, trace the ENTIRE pipeline (`claude_parse` → post-parse → `validate_sport` → `build_context` → fetcher → `claude_grade`) before fixing.
- Always use `--channel <id>` when running `tracker.py --live` locally — unscoped runs broadcast to production.
- **Log queries: mind the timezone.** `journalctl --since/--until` uses the VPS **local time (America/New_York, EDT)**, but `graded_at` (DB/cache) and Telegram `ts` are **UTC**. Subtract 4h (UTC→EDT) or the query returns "-- No entries --" and you'll wrongly conclude a service was idle. Confirm with `timedatectl`.
- **`systemctl is-active` lies about hangs.** A wedged process (e.g. blocked on an untimed network call) still shows `active`. Verify liveness by the recency of its last log line, or `systemctl show <svc> -p WatchdogTimestamp` — not `is-active` alone.
- **Broadcasting is daemon-only.** The tracker grades + edits emojis but must never set `broadcasted=True` (it doesn't broadcast). If a result got its emoji but no broadcast, suspect the daemon was down when the tracker graded it — check the daemon's log continuity around the grade time.
- **`send_as_user` channels are NOT bot-editable.** Their messages are sent by the Telethon userbot, so the Bot API returns `400: message can't be edited`. Only the tracker can edit them (via its `_user_edit_message` Telethon fallback for `user_edit_channels`). The grade daemon (Bot-API-only) skips these channels entirely. If a pick in such a channel graded but shows no emoji, it's this class of bug — check for `edit failed <ch>:<msg>` in the daemon log.
- **Don't trust cached `html_text` to decide if a pick is "ungraded".** Old cache entries often have empty/stale `html_text`, so scanning the cache for "no emoji" produces false positives. Verify against the **live** Telegram message text (`vps_msg.py` / `iter_messages`) before concluding a pick is stuck.
- **To force a re-parse, invalidate ALL sibling cache entries sharing `_source_key`, not just one.** A forwarded pick appears in multiple channels; each channel's entry (`<ch>:<msg>`) is a mirror of one source (`_source_key`, e.g. `1910823870:426471`). The tracker re-populates an invalidated entry by copying `parsed`+`odds` from any sibling that still has it (`[mirror] reusing parse+odds from …`). Delete `parsed`/`odds_by_pick`/`leg_verdicts` from **every** entry with the same `_source_key` in one write, keeping `_forwarded` (so it won't re-forward), then re-run the tracker per channel.
- **A pick stuck as `sport: Other` with `odds_match_type: sport_unsupported(Other)` usually means the sport isn't wired in, not a parse bug.** Adding a sport = enum in `ai.py` + `ESPN_LEAGUES` (`scores.py`) + `SPORT_KEYS`/`PROP_STAT_MARKETS`/`HALF_POINT_COST` (`odds.py`) + tracker parlay-validation whitelist. See [[wnba-support]].

## Self-improvement

After resolving, review mistakes and add lessons above and/or to CLAUDE.md. Save relevant feedback to memory. If a lesson describes a code bug, fix the code instead of adding the lesson — lessons should document unavoidable constraints, not workarounds.
