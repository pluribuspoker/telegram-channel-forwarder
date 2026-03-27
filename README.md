# Telegram Channel Forwarder

Automatically re-posts messages from source Telegram channels/topics to destination channels. Messages appear as original posts with no "forwarded from" tag. Supports text, photos, documents, and photo albums.

Also includes a **pick grader** (`tracker.py`) that runs every 5 minutes, grades sports betting picks in destination channels, and appends ✅/❌ inline after each pick line.

---

## Components

| File | Purpose |
|---|---|
| `listener.py` | Real-time forwarder — runs persistently as a systemd service |
| `tracker.py` | Pick grader entry point + orchestration (CLI, live mode, backtest, Telegram editing) |
| `scores.py` | Sports data — ESPN / Odds API fetching, scoreboard formatting, team matching |
| `ai.py` | Claude AI — pick parsing, grading, context building, cost tracking |
| `audit.py` | Audit log — writes to SQLite + Telegram audit channel |
| `common.py` | Shared utilities (Anthropic client, OCR, channel parsing, emoji map) |
| `run_tracker.sh` | Timer wrapper with retry logic and healthchecks.io signals |

---

## Forwarder

`listener.py` runs persistently and forwards messages in real-time (~2 second latency). A user account reads from source channels (supports private channels). A bot posts to destination channels (required for push notifications).

### Setup

**Prerequisites:**
- Python 3.11+
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- A Telethon `StringSession` — run `python scripts/get_session.py` to generate
- A bot token from [@BotFather](https://t.me/botfather) — add bot as admin to destination channels

**Install:**
```bash
pip install -r requirements.txt
```

**Configure `.env`:**
```env
# Telegram API
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_SESSION=your_session_string
BOT_TOKEN=your_bot_token

# Channel config
MAPPINGS_CONFIG='[
  {
    "id": "my-mapping",
    "source_channel": -100xxxxxxxxxx,
    "source_topic_id": null,
    "dest_channel": -100xxxxxxxxxx,
    "test_source_channel": -100xxxxxxxxxx,
    "test_dest_channel": -100xxxxxxxxxx
  }
]'
GRADE_CHANNELS=[-100xxxxxxxxxx, -100xxxxxxxxxx]
AUDIT_CHANNEL_ID=-100xxxxxxxxxx

# AI / external APIs
ANTHROPIC_API_KEY=your_key
ODDS_API_KEY=your_key

# Healthchecks (healthchecks.io)
LISTENER_HEALTHCHECK_URL=https://hc-ping.com/your-uuid
TRACKER_HEALTHCHECK_URL=https://hc-ping.com/your-uuid
```

**Find channel IDs:**
```bash
python scripts/list_channels.py
```

**Run:**
```bash
python listener.py         # production mode
python listener.py --test  # uses test_source/dest channels
```

### Mapping options

**`filter_pattern`** — regex applied to message text; only matching messages are forwarded. Omit to forward everything.
```json
"filter_pattern": "(?i)^[A-Za-z][A-Za-z ]*:[ ]*[(]"
```

**`ocr_odds`** — extracts American odds from a bet slip screenshot via Claude Haiku and appends to caption. Image dropped on success; kept as fallback on failure. Requires `ANTHROPIC_API_KEY`.
```json
"ocr_odds": true
```

**`source_topic_id`** — optional, for forum/topic channels only.

### Logging
```
 15:43:26  ✦ SENT     ┃  STRAIGHT: (1 UNIT)  UCLA +6.5  [ocr: -146]
 18:33:27  ✦ SENT     ┃  STRAIGHT: (1 UNIT)  NC STATE +11.5  [ocr: failed → [photo]]
 15:43:50  · filtered ┃  Putting half the OSU winnings on it
```

---

## Pick Grader

`tracker.py` grades sports picks by fetching game results from ESPN and using Claude Sonnet to determine win/loss. Appends ✅/❌ inline after each pick line in the Telegram message, preserving original formatting.

**Sports supported:** NBA, NCAAB, MLB, NFL, NHL, NCAAF, UFC, UFL, Tennis (ESPN core API), Boxing (Odds API)

**Verdict types:**
- `✅ WIN` / `❌ LOSS` / `↩️ PUSH` — graded, message edited in Telegram
- `⏳ PENDING` — game found in ESPN but not yet completed
- `❓ UNKNOWN` — game not found or sport not supported

### Usage

```bash
# Grade picks from a Telegram JSON export (backtest — output goes to data/)
python tracker.py --backtest data/result.json

# Grade a single pick interactively
python tracker.py --grade "Hawks +3.5" --date 2026-03-26

# Live mode — scan channels and edit messages
python tracker.py --live                          # last 1 day (default)
python tracker.py --live --days 7                 # last 7 days
python tracker.py --live --dry-run                # preview without editing
python tracker.py --live --channel -100xxxxxxxxxx # single channel only
```

### Backtest accuracy

| Channel | Accuracy | Skipped |
|---|---|---|
| DF | 97% (76/78) | 2 (UFC — ESPN data unavailable at test time) |
| Cappers Lab | 100% (16/16) | 0 |

### Audit log

Every grade action writes to `picks.db` (SQLite) and posts to a private Telegram audit channel (`AUDIT_CHANNEL_ID`). Messages show capper, channel, pick with verdict emoji, sport, game date, and calc. Dry runs tagged `[DRY]`. PENDING picks written to DB only (not posted to audit channel).

### Parse cache

`parse_cache.json` caches `claude_parse` results for pending messages. Since the grader runs every 5 minutes, this avoids redundant Claude API calls for picks whose games haven't started yet. Evicted automatically when a pick is graded.

### Production (VPS)

`run_tracker.sh` wraps the grader with:
- 2 retry attempts (60s apart) on failure
- Healthchecks.io start/success/fail signals (`TRACKER_HEALTHCHECK_URL`)

Deployed as a systemd timer firing every 5 minutes. Manual runs via server aliases:
```bash
grade      # live, last 1 day
gradetest  # dry run, last 2 days
```

---

## VPS deployment

See `CLAUDE.md` for server aliases, deploy workflow, and switching to test mode.

```powershell
# Local — commit, push, and deploy in one step
git add -A && git commit -m "..."
ship   # pushes to GitHub + runs deploy on server
```
