# Telegram Channel Forwarder

Automatically re-posts messages from source Telegram channels/topics to destination channels. Messages appear as original posts with no "forwarded from" tag. Supports text, photos, documents, and photo albums.

## How it works

Two modes:

- **`listener.py`** — runs persistently, forwards messages in real-time (~2 seconds latency)
- **`forwarder.py`** — polling script, designed for scheduled runs (e.g. GitHub Actions)

A user account reads from source channels (supports private channels). A bot posts to destination channels (required for push notifications).

## Setup

### 1. Prerequisites

- Python 3.11+
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- A Telethon `StringSession` (run `python get_session.py` to generate one)
- A bot token from [@BotFather](https://t.me/botfather) — add the bot as admin to destination channels

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure `.env`

```env
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_SESSION=your_session_string
BOT_TOKEN=your_bot_token
MAPPINGS_CONFIG='[
  {
    "id": "my-mapping",
    "source_channel": -100xxxxxxxxxx,
    "source_topic_id": null,
    "dest_channel": -100xxxxxxxxxx,
    "test_source_channel": -100xxxxxxxxxx,
    "test_dest_channel": -100xxxxxxxxxx,
    "filter_pattern": "(?i)^[A-Za-z][A-Za-z ]*:[ ]*[(]"
  }
]'
```

- `source_topic_id` — optional, for forum/topic channels only
- `test_source_channel` / `test_dest_channel` — optional, used with `--test` flag

### 4. Find channel IDs

```bash
python list_channels.py
```

## Running locally

```bash
# Real-time listener (recommended)
python listener.py

# Test mode (uses test_source/dest channels)
python listener.py --test

# Polling mode
python forwarder.py --limit 50
python forwarder.py --clear-state        # reset all state
python forwarder.py --clear-state my-id  # reset one mapping
```

## GitHub Actions

Runs `forwarder.py` on a 5-minute cron schedule.

### Required secrets

| Secret | Description |
|---|---|
| `TELEGRAM_API_ID` | From my.telegram.org |
| `TELEGRAM_API_HASH` | From my.telegram.org |
| `TELEGRAM_SESSION` | Telethon StringSession |
| `BOT_TOKEN` | Bot token from BotFather |
| `MAPPINGS_CONFIG` | Minified JSON mappings array |

### Manual trigger options

- **Destination** — `real` or `test`
- **Clear all state** — checkbox to reset all mapping state
- **Clear state for specific mapping** — enter a mapping ID

## Adding a new mapping

Update `MAPPINGS_CONFIG` in `.env` and in the GitHub secret — no code changes needed.
