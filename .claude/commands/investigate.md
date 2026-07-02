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

Never say "I can't access Telegram links." SSH into the VPS and use Telethon to read any message:

```python
import asyncio, os
from telethon import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv

load_dotenv("/home/forwarder/app/.env")
load_dotenv("/home/forwarder/app/.env.local", override=True)
api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
session_str = os.environ["TELEGRAM_SESSION"]

async def main():
    client = TelegramClient(StringSession(session_str), api_id, api_hash)
    await client.connect()
    # Fetch message, read channel, etc.
    await client.disconnect()

asyncio.run(main())
```

Write the script to a temp file on the VPS and run it as forwarder, rather than inlining Python in shell quotes.

### 3. Don't give up after one failed attempt

If an API call fails, a query returns nothing, or data seems missing — try variations before concluding it doesn't exist. Check different key formats, parameter names, date ranges, etc.

### 4. Investigation workflow

1. **Gather context**: Read the user's description, fetch the relevant Telegram message if linked, check parse_cache and/or DB on VPS
2. **Read the relevant code locally**: Understand the code path involved before theorizing
3. **Check VPS logs** if the issue involves runtime behavior (grading, broadcasting, odds, etc.)
4. **Identify root cause**: Trace the bug through the code with the data you gathered
5. **Fix the code** and verify the fix (see testing section below)
6. **Fix the live data** if needed (e.g., correct a wrong emoji on a message, fix a DB entry) — but **never restart the service**
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

### 6. Push and deploy when confident

If you are confident in the fix and have verified it, push and deploy yourself: `git push`, then SSH to VPS and run `cd /home/forwarder/app && git pull && systemctl restart telegram-forwarder`. If unsure, let the user handle it.
