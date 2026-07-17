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

After resolving, review whether this investigation revealed any **generalizable** lessons — operational traps, debugging principles, or constraints that apply broadly across unrelated future investigations. If yes, add them above. If the fix was code-only (a bug you fixed in the source), or the mistake was too incident-specific to recur, **skip this step** — no lesson needed. Do not add a lesson just to have one.
