---
description: Debug and investigate issues with live data, logs, and Telegram messages
argument-hint: <issue description, Telegram link, or error message>
---

# Investigate

$ARGUMENTS

## Rules

1. **Live data is on the VPS, not local.** SSH to VPS for logs, DB, parse cache (see CLAUDE.md for paths/aliases/deploy).
2. **Telegram messages are accessible.** Use `scripts/vps_msg.py` on VPS. For complex queries, write a temp script locally, `scp` it, run as forwarder.
3. **Don't give up after one failed attempt.** Try variations before concluding data doesn't exist.
4. **Use a git worktree for code changes** (see CLAUDE.md worktree pattern). Do ALL commits in the worktree before merging.

## Workflow

1. **Gather context**: Fetch the Telegram message if linked, check parse_cache and/or DB on VPS
2. **Read the relevant code locally** before theorizing
3. **Verify VPS matches local**: `ssh root@209.38.51.86 'cd /home/forwarder/app && git log --oneline -1'` — VPS helper scripts may not exist yet if versions differ
4. **Check VPS logs** (`journalctl -u telegram-tracker`, `journalctl -u grade-daemon`, etc.)
5. **Identify root cause**: Trace the full pipeline before fixing
6. **Fix and verify** with the real data that triggered the bug (replay actual inputs through the fixed code)
7. **Deploy code fix first** (push + deploy) before touching live data
8. **Fix live data** if needed (wrong emoji, DB entry, etc.). Use Bot API with `parse_mode: "HTML"` — check `msg.media`: use `editMessageCaption` for photo/video, `editMessageText` for plain text
9. **Run tracker manually** on VPS scoped to the affected channel for instant verification:
   `su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --channel <channel_id>"`

## VPS queries

```bash
# Telegram message
su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/vps_msg.py <channel_id> <msg_id>"
# Grades DB
su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/vps_grades.py --msg-id <id>"
su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/vps_grades.py --search <term>"
```

## Lessons

- Never inline Python in SSH commands beyond simple one-liners. Write a temp script, `scp` it, run it.
- For grading bugs, trace the ENTIRE pipeline (`claude_parse` → post-parse → `validate_sport` → `build_context` → fetcher → `claude_grade`) before fixing.
- Always use `--channel <id>` when running `tracker.py --live` locally — unscoped runs broadcast to production.

## Self-improvement

After resolving, review mistakes and add lessons above and/or to CLAUDE.md. Save relevant feedback to memory. If a lesson describes a code bug, fix the code instead of adding the lesson — lessons should document unavoidable constraints, not workarounds.
