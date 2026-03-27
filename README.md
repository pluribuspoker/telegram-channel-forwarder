# Telegram Channel Forwarder

Automatically re-posts messages from source Telegram channels/topics to destination channels. Messages appear as original posts with no "forwarded from" tag. Supports text, photos, documents, and photo albums.

Also includes a **pick grader** (`tracker.py`) that nightly grades sports betting picks in destination channels by appending ✅/❌ to each pick line.

---

## Components

| File | Purpose |
|---|---|
| `listener.py` | Real-time forwarder — runs persistently as a systemd service |
| `tracker.py` | Pick grader — runs nightly via systemd timer |
| `audit.py` | Audit log for tracker — writes to SQLite + Telegram audit channel |
| `run_tracker.sh` | Nightly wrapper with retry logic and healthchecks.io signals |

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
Example matches `STRAIGHT: (1 UNIT)`, `PARLAY: (2 UNITS)`.

**`ocr_odds`** — when `true`, extracts American odds from a bet slip screenshot via Claude Haiku and appends to caption. Image is dropped on success; kept as fallback on failure.
```json
"ocr_odds": true
```
Requires `ANTHROPIC_API_KEY`. Output example: `STRAIGHT: (1 UNIT)\n\nUCLA +6.5 -146`

**`source_topic_id`** — optional, for forum/topic channels only.

**`test_source_channel` / `test_dest_channel`** — optional, used with `--test` flag.

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
- `✅ WIN` / `❌ LOSS` / `↩️ PUSH` — graded and edited in Telegram
- `⏳ PENDING` — game found in ESPN but not yet played
- `❓ UNKNOWN` — game not found or sport not supported

### Usage

```bash
# Grade picks from a Telegram export (backtest / accuracy check)
python tracker.py --backtest result.json

# Grade a single pick interactively
python tracker.py --grade "Hawks +3.5" --date 2026-03-26

# Live mode — scan channels and edit messages
python tracker.py --live --days 2              # last 2 days
python tracker.py --live --days 365            # full backfill
python tracker.py --live --dry-run --days 2    # preview without editing
python tracker.py --live --channel -100xxx     # single channel only
```

### Backtest accuracy

| Channel | Accuracy | Skipped |
|---|---|---|
| DF | 97% (76/78) | 2 (UFC — ESPN data unavailable) |
| Cappers Lab | 100% (16/16) | 0 |

### Audit log

Every grade action writes to `picks.db` (SQLite) and posts to a private Telegram audit channel (`AUDIT_CHANNEL_ID`). Audit messages show capper, channel, pick with verdict emoji, sport, game date, and calc — in HTML format.

Dry runs are recorded with `dry_run=1` and tagged `[DRY]` in the audit channel.

### Nightly cron (VPS)

`run_tracker.sh` wraps the grader with:
- Up to 3 retry attempts (5 min apart) on failure
- Healthchecks.io start/success/fail signals (`TRACKER_HEALTHCHECK_URL`)

Deployed as a systemd timer firing at 3 AM ET. Manual runs via server aliases:
```bash
grade       # live, last 2 days
gradetest   # dry run, last 2 days
```

---

## VPS deployment

See `CLAUDE.md` for server aliases, deploy workflow, and switching to test mode.

```powershell
# Local — commit, push, and deploy in one step
git add -A && git commit -m "..."
ship   # pushes to GitHub + runs deploy on server
```
