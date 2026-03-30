# Telegram Channel Forwarder

Automatically re-posts messages from source Telegram channels/topics to destination channels. Messages appear as original posts with no "forwarded from" tag. Supports text, photos, documents, and photo albums.

Also includes a **pick grader** (`tracker.py`) that runs every 5 minutes, grades sports betting picks in destination channels, and appends ✅/❌ inline after each pick line.

---

## Components

| File | Purpose |
|---|---|
| `listener.py` | Real-time forwarder — runs persistently as a systemd service |
| `tracker.py` | Pick grader entry point + orchestration (CLI, live mode, backtest, Telegram editing) |
| `odds.py` | Odds lookup — fetches pre-game lines from Odds API, caches in `picks.db`, sanity checks |
| `scores.py` | Sports data — ESPN / Odds API fetching, scoreboard formatting, team matching |
| `ai.py` | Claude AI — pick parsing, grading, context building, cost tracking |
| `audit.py` | Audit log — writes to SQLite + Telegram audit channel |
| `common.py` | Shared utilities (Anthropic client, OCR, channel parsing, emoji map, regulation ML detection) |
| `run_tracker.sh` | Timer wrapper with retry logic and healthchecks.io signals |

---

## Forwarder

`listener.py` runs persistently and forwards messages in real-time (~2 second latency). A user account reads from source channels (supports private channels). A bot posts to destination channels (required for push notifications).

### Setup

**Prerequisites:**
- Python 3.11+
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
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
BOT_TOKEN=your_bot_token

# Channel config
MAPPINGS_CONFIG='[
  {
    "id": "my-mapping",
    "source_channel": -100xxxxxxxxxx,
    "source_topic_id": null,
    "dest_channel": -100xxxxxxxxxx,
    "test_source_channel": -100xxxxxxxxxx,
    "test_dest_channel": -100xxxxxxxxxx,
    "broadcast_results_channel": -100xxxxxxxxxx,
    "test_broadcast_results_channel": -100xxxxxxxxxx
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

**Generate session strings (writes to `.env.local` automatically):**
```bash
python scripts/get_session.py      # TELEGRAM_SESSION — user account
python scripts/get_bot_session.py  # BOT_SESSION — bot account
```
Run these on each machine separately (local and VPS) — sessions are tied to the IP.

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

**`filter_pattern`** — regex applied to message text; only matching messages are forwarded. Omit to forward everything. Bypassed in `--test` mode so any message triggers a forward.
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

**Sports supported:** NBA, NCAAB, MLB, NFL, NHL, NCAAF, UFC, UFL, Tennis (ESPN core API)

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

### Odds

The tracker fetches odds from the Odds API at first encounter. `listener.py` triggers a tracker run ~3 seconds after forwarding each pick, so odds typically appear within 15–30 seconds. The regular 5-minute systemd run serves as a backstop. Odds are:
- Edited into the destination message immediately: `Hawks +3.5 [-115]`
- Preserved through the grading edit: `Hawks +3.5 [-115]✅`
- Included in broadcast messages: `✅ Hawks +3.5 [-115] · Capper`
- Stored in `picks.db` (`grades.odds`) for audit

Tracker-fetched odds use **square brackets** `[-115]` to distinguish them from odds the capper wrote themselves `(-115)`.

**When a game is already in progress** at tracking time, the tracker fetches both:
- **Live odds** — current in-game line from the Odds API (updates with a 5-min cache): `[-120 live]`
- **Pre-game closing line** — historical snapshot at game start time: `[-130 pre]`

Both are shown together when available: `Stars/Flyers U5.5 [-120 live · -130 pre]`. Falls back to pre-game only (`[-130 pre]`) if live odds are unavailable, or to a silent miss if neither is found.

Any unexpected failure to find odds posts one warning to the Telegram audit channel (never repeated for the same pick). Requires `ODDS_API_KEY` in `.env`.

**Sports with odds coverage:** NBA, NCAAB, MLB, NFL, NHL, NCAAF, UFC, UFL (~91% of recent picks; MLB F5 innings and small UFC cards are structural gaps)

### Broadcast results

After grading, the tracker posts a compact result message to the `broadcast_results_channel` configured in each mapping. Only WIN and LOSS verdicts are broadcast. Format:

```
✅ Duke -4.5 [-153] · Travy
❌ Calgary Flames ML [+113] · NY Sharps
✅ Mariners/Guardians U7 [-108] · Smart Money Sports

Andrew Cunningham
✅ Birmingham Stallions ML [-175]
❌ Birmingham Stallions -3.5 [-110]

✅ Cesar exclusive · Parlay
• Hawks +10.5
• Raptors ML
```

- Capper name is a bold hyperlink back to the original pick
- Odds shown inline when available; omitted gracefully if not found
- Descriptions standardized: `ML` shorthand (`3-way ML` for regulation/3-way moneylines), `Team1/Team2 O/U` for game totals, period tags (`1H`, `2H`)
- `--dry-run` routes to `test_broadcast_results_channel` for safe previewing

**Reset emojis for re-testing:**
```bash
python scripts/clear_emojis.py --channel -100xxxxxxxxxx  # today
python scripts/clear_emojis.py --days 2                  # last 2 days
```

### Audit log

Every grade action writes to `picks.db` (SQLite) and posts to a private Telegram audit channel (`AUDIT_CHANNEL_ID`). Messages show capper, channel, pick with verdict emoji, sport, game date, and calc. Dry runs tagged `[DRY]`. PENDING picks written to DB only (not posted to audit channel).

### Parse cache

`parse_cache.json` caches `claude_parse` results for pending messages. Since the grader runs every 5 minutes, this avoids redundant Claude API calls for picks whose games haven't started yet. Evicted automatically when a pick is graded.

Messages that parse successfully but contain no picks (e.g. "sorry, no picks today") are cached with `{"_failed": True, "text_hash": ...}`. Subsequent runs skip Claude entirely for these and just show a `⚠` warning — no repeated API cost. If the capper edits the message, the hash changes and Claude retries automatically.

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
