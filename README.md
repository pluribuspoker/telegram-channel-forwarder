# Telegram Channel Forwarder

Automatically re-posts messages from source Telegram channels/topics to destination channels. Messages appear as original posts with no "forwarded from" tag. Supports text, photos, documents, and photo albums.

## How it works

`listener.py` runs persistently and forwards messages in real-time (~2 seconds latency). A user account reads from source channels (supports private channels). A bot posts to destination channels (required for push notifications).

## Setup

### 1. Prerequisites

- Python 3.11+
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- A Telethon `StringSession` (run `python scripts/get_session.py` to generate one)
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
ANTHROPIC_API_KEY=your_anthropic_api_key
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
```

- `source_topic_id` — optional, for forum/topic channels only
- `test_source_channel` / `test_dest_channel` — optional, used with `--test` flag
- `ANTHROPIC_API_KEY` — only required if any mapping uses `ocr_odds`

### 4. Find channel IDs

```bash
python scripts/list_channels.py
```

## Running

```bash
python listener.py         # real mode
python listener.py --test  # uses test_source/dest channels
```

## Mapping options

Each object in `MAPPINGS_CONFIG` supports these optional fields:

### `filter_pattern`

A regex applied to message text. Only messages (or albums) where at least one message matches are forwarded. Omit to forward everything.

```json
"filter_pattern": "(?i)^[A-Za-z][A-Za-z ]*:[ ]*[(]"
```

The example matches picks in the format `WORD(S): (anything)` — e.g. `STRAIGHT: (1 UNIT)`, `PARLAY: (2 UNITS)`.

### `ocr_odds`

When `true`, the attached image on a matched message is sent to Claude Haiku, which extracts the American odds (e.g. `-146`, `+220`) from the bet slip screenshot. If successful, the odds are appended to the caption and the image is dropped — the forwarded message is text only. If OCR fails, the original image is kept as a fallback.

```json
"ocr_odds": true
```

Requires `ANTHROPIC_API_KEY`. Example output: `STRAIGHT: (1 UNIT)\n\nUCLA +6.5 -146`

## Logging

```
 15:43:26  ✦ SENT     ┃  STRAIGHT: (1 UNIT)  UCLA +6.5  [ocr: -146]
 18:33:27  ✦ SENT     ┃  STRAIGHT: (1 UNIT)  NC STATE +11.5  [ocr: failed → [photo]]
 15:43:50  · filtered ┃  Putting half the OSU winnings on it
```

## Adding a new mapping

Update `MAPPINGS_CONFIG` in `.env` — no code changes needed.
