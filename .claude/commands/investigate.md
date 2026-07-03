---
description: Debug and investigate issues with live data, logs, and Telegram messages
argument-hint: <issue description, Telegram link, or error message>
---

# Investigate

Debug and investigate issues in the Telegram Channel Forwarder project.

## Arguments

$ARGUMENTS - Description of the issue, optionally with a Telegram message link, screenshot, or error message.

## Instructions

You are investigating a bug or issue in the Telegram Channel Forwarder. Follow these rules:

### 1. Live data lives on the VPS, not locally

**Always SSH into the VPS (`ssh root@209.38.51.86`) to access:**
- Service logs: use `flogs` / `tlogs` aliases, or `journalctl -u telegram-forwarder`
- Database: `picks.db` is at `/home/forwarder/app/picks.db`
- Parse cache: `/home/forwarder/app/parse_cache.json`
- Cron logs: `/tmp/sauce_daily_cron.log`
- Any runtime state or data files

Run commands as the forwarder user when needed: `su - forwarder -c "cd ~/app && ..."`

The local repo has the code but no live data. Don't try to find logs, DB, or cache locally.

### 2. Telegram messages are accessible

Never say "I can't access Telegram links." SSH into the VPS, write a temp Python script that uses Telethon with the session from `.env.local`, and run it as forwarder. Use `StringSession` — do not inline Python in shell quotes.

### 3. Don't give up after one failed attempt

If an API call fails, a query returns nothing, or data seems missing — try variations before concluding it doesn't exist. Check different key formats, parameter names, date ranges, etc.

### 4. Investigation workflow

1. **Gather context**: Read the user's description, fetch the relevant Telegram message if linked, check parse_cache and/or DB on VPS
2. **Read the relevant code locally**: Understand the code path involved before theorizing
3. **Verify deployed version matches local**: Run `ssh root@209.38.51.86 'cd /home/forwarder/app && git log --oneline -1'` and compare with local `git log --oneline -1` before assuming the VPS is running the code you're reading
4. **Check VPS logs** if the issue involves runtime behavior (grading, broadcasting, odds, etc.)
5. **Identify root cause**: Trace the bug through the code with the data you gathered
6. **Fix the code** and verify the fix (see testing section below)
7. **Deploy the code fix first** (push + deploy) before touching live data — the running tracker will overwrite live edits if the buggy code is still active.
8. **Fix the live data** if needed (e.g., correct a wrong emoji on a message, fix a DB entry).
   - To edit Telegram messages, use Bot API `editMessageText` with `parse_mode: "HTML"` — Telethon `edit_message` strips formatting.

### 5. Testing and verification

**Use the real data that triggered the bug.** After identifying root cause and writing a fix, verify it end-to-end by replaying the same inputs that caused the failure:

- Extract the actual message text, parse cache entry, or API response that exposed the bug
- Write a test that feeds that exact data through the fixed code path and asserts the correct result
- Don't just test the happy path — confirm the specific scenario that broke

**Send test messages to the TEST channel only.** If verification requires sending or editing Telegram messages (e.g., testing emoji placement, broadcast formatting), use the test channel — never the production channel. Use `--test` mode when running `listener.py` or `tracker.py` locally.

**Verify both the code fix and the live correction.** When a bug produced wrong output on a live message (wrong emoji, wrong label, wrong odds), fix both:
1. The code, so it won't happen again
2. The live message/data on the VPS, so the current state is correct

### 6. Common queries on VPS

**Parse cache** (find a pick by text substring):
```bash
su - forwarder -c "cd ~/app && python -c \"import json; d=json.load(open('parse_cache.json')); [print(k,v['parsed']['pick']) for k,v in d.items() if 'SUBSTRING' in v.get('parsed',{}).get('pick','')]\""
```

**Picks DB** (recent picks with grading status):
```bash
su - forwarder -c "cd ~/app && sqlite3 picks.db \"SELECT pick, result, sport, timestamp FROM picks ORDER BY timestamp DESC LIMIT 20\""
```

### 7. Deploy command

Deploy: `git push`, then SSH to VPS and run `cd /home/forwarder/app && git pull && systemctl restart telegram-forwarder`. If unsure about the fix, let the user handle deploy.

### 8. Self-improvement

After resolving an investigation, consider whether a mistake you made could be avoided with a better rule **in this file or in CLAUDE.md**. Add investigation-specific rules here; add project-wide rules to CLAUDE.md. Don't duplicate between the two. One line per lesson.
