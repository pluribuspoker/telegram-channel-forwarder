# Telegram Intake Form → Google Sheets — Plan

## Goal

Add a Telegram-based intake system. A user DMs the bot a command such as
`/intake-wnba`, the bot presents the day's available WNBA games (read live from a
Google Sheet), the user taps a game, the bot shows that game's info (spread &
totals, read from a Google Sheet), the user replies with a free-text prediction
(assumed to start with `Total: ` or `Spread: `), and the submission is appended
to a **new dedicated intake Google Sheet/tab**.

Reuses the Google Sheets access patterns proven in the `line-movement/` repo
(service-account auth + read/write rate-limit buckets + 429 backoff).

---

## User flow (end-to-end experience)

The walkthrough below is a WNBA example: the user asks for today's games, the bot
lists them and asks which one, the user selects a game, the bot shows game detail,
and the user submits a prediction via a **type dropdown** (`Total` / `Spread` /
`Moneyline` / `Other`) with a free-text **input box** next to it.

```
 ┌──────────────────────────────────────────────────────────────┐
 │  Chat with  @IntakeBot                                        │
 ├──────────────────────────────────────────────────────────────┤
 │                                                              │
 │                                    ┌───────────────────────┐  │
 │                                    │  /intake-wnba         │  │  ← user
 │                                    └───────────────────────┘  │
 │                                                              │
 │  ┌────────────────────────────────────────────────┐          │
 │  │ 🏀  WNBA — games today (Thu Jul 16)             │          │  ← bot
 │  │ Which game do you want to predict?              │          │
 │  │                                                 │          │
 │  │  ┌───────────────────────────────────────────┐ │          │
 │  │  │  Aces @ Liberty        · 7:00 PM ET       │ │  ◄ tap    │
 │  │  ├───────────────────────────────────────────┤ │          │
 │  │  │  Sky @ Sun             · 7:30 PM ET       │ │          │
 │  │  ├───────────────────────────────────────────┤ │          │
 │  │  │  Mercury @ Storm       · 10:00 PM ET      │ │          │
 │  │  ├───────────────────────────────────────────┤ │          │
 │  │  │  Fever @ Wings         · 8:00 PM ET       │ │          │
 │  │  └───────────────────────────────────────────┘ │          │
 │  │        (inline keyboard — one button per game)  │          │
 │  └────────────────────────────────────────────────┘          │
 │                                                              │
 │                                    ┌───────────────────────┐  │
 │                                    │ (taps “Aces @ Liberty”)│ │  ← user
 │                                    └───────────────────────┘  │
 │                                                              │
 │  ┌────────────────────────────────────────────────┐          │
 │  │ 📋  Aces @ Liberty — 7:00 PM ET                 │          │  ← bot
 │  │ ─────────────────────────────────────────────   │          │
 │  │   Spread :  NY -4.5                             │          │
 │  │   Total  :  165.5                               │          │
 │  │   ML     :  LV +160 / NY -190                   │          │
 │  │ ─────────────────────────────────────────────   │          │
 │  │ Enter your prediction:                          │          │
 │  │                                                 │          │
 │  │   Type ▾            Your prediction             │          │
 │  │  ┌───────────────┐ ┌───────────────────────────┐│          │
 │  │  │ Total       ▾ │ │ Under 165.5 — slow pace,  ││  ◄ type   │
 │  │  │───────────────│ │ both D's elite            ││    +      │
 │  │  │ Total         │ └───────────────────────────┘│    input  │
 │  │  │ Spread        │        [ Submit ]             │          │
 │  │  │ Moneyline     │                               │          │
 │  │  │ Other         │                               │          │
 │  │  └───────────────┘                               │          │
 │  └────────────────────────────────────────────────┘          │
 │                                                              │
 │                                    ┌───────────────────────┐  │
 │                                    │ Type: Total           │  │  ← user
 │                                    │ Under 165.5 — slow…   │  │
 │                                    └───────────────────────┘  │
 │                                                              │
 │  ┌────────────────────────────────────────────────┐          │
 │  │ ✅  Logged your prediction                       │          │  ← bot
 │  │   Aces @ Liberty · Total                        │          │
 │  │   “Under 165.5 — slow pace, both D's elite”     │          │
 │  │   → saved to intake sheet                       │          │
 │  └────────────────────────────────────────────────┘          │
 │                                                              │
 └──────────────────────────────────────────────────────────────┘
```

### How the “dropdown + input box” maps to Telegram

Telegram DMs have **no native side-by-side dropdown-with-textbox widget**, so the
mockup above is the *intended experience*; it maps to one of two implementations
(see "Design decision — form UI mechanism"):

- **Native (v1, no hosting):** the **Type** dropdown becomes an inline-button row
  (`[ Total ] [ Spread ] [ Moneyline ] [ Other ]`); tapping one opens a
  `ForceReply` **input box** pre-labeled with that type, where the user types the
  value + reason. Two taps + one text entry — the closest native analog.
- **Telegram Web App (later):** a real HTML form renders the dropdown and text box
  exactly as drawn, submitting both fields at once. Requires hosting + a bot
  domain; natural upgrade if the form grows.

Text-transcript form of the same flow:

```
User: /intake-wnba
Bot:  "WNBA — games today:"        [inline keyboard, one button per game]
        [ Aces @ Liberty  7:00 PM ] [ Sky @ Sun 7:30 PM ] ...
User: (taps "Aces @ Liberty")
Bot:  "Aces @ Liberty — 7:00 PM ET
       Spread: NY -4.5 | Total: 165.5 | ML: LV +160 / NY -190
       Choose a prediction type:"  [ Total ][ Spread ][ Moneyline ][ Other ]
User: (taps "Total")
Bot:  "Total — reply with your prediction + reason:"   [ForceReply]
User: (replies) "Under 165.5 — slow pace, both D's elite"
Bot:  "✅ Logged. Aces @ Liberty · Total · Under 165.5 …"
      → row appended to the intake sheet
```

---

## Architecture

```
intake_bot.py            ← new: Telethon bot, command + callback + reply handlers,
                            in-memory conversation state, allowlist gate
intake_sheets.py         ← new: read game list + game info from source sheet(s),
                            append submission to the intake sheet
                            (adapts line-movement/sheets_utils.py patterns)
deploy/systemd/
  telegram-intake.service ← new: runs intake_bot.py under the forwarder user
run_intake_bot.sh        ← new: venv launcher (mirrors run_grade_daemon.sh)
```

### Design decision — process isolation

**Options considered:**
- **A. Separate process/systemd service (recommended, adopted).** A standalone
  bot process, Bot-API-only (`BOT_SESSION` / `BOT_TOKEN`), like `grade_daemon.py`.
- **B. Add handlers to the existing `listener.py` bot.** Reuse the already-running
  bot client and event loop.
- **C. Extend the existing Telegram Channels Claude bot** (the tmux `claude`
  session) to handle intake.

**Recommendation: A.** The forwarder `listener.py` runs a persistent Telethon
**user** session and is flood-wait sensitive (`CLAUDE.md`: "Deploy cautiously.
Rapid bot session restarts trigger Telegram flood waits"). Adding stateful,
frequently-iterated command handlers there means every intake code change forces
a listener restart, risking flood-waits and forwarding downtime. Option C couples
intake to an interactive AI session that has no history/backfill (dropped-message
risk noted in `CLAUDE.md`) and isn't a deterministic form handler. Option A keeps
intake **Bot-API-only** (zero Telethon-user/session risk, exactly the isolation
rationale behind `grade_daemon.py`), independently deployable and restartable, and
a crash/flood-wait on either side can't take down the other.

**Trade-off accepted:** one more service to run and monitor, and a second Bot API
poll loop. Worth it for fault isolation.

### Design decision — bot framework

**Options considered:**
- **A. Telethon (recommended, adopted).** Already the repo standard
  (`requirements.txt: telethon>=1.42.0`), with bot-session tooling
  (`scripts/get_bot_session.py`) and established patterns in `listener.py`.
- **B. `python-telegram-bot` / `aiogram`.** Popular, higher-level conversation
  and keyboard abstractions (e.g. PTB `ConversationHandler`).
- **C. Raw Bot API over HTTP** (`httpx`, already a dep) with manual long-polling.

**Recommendation: A.** Telethon already provides everything the flow needs —
inline buttons (`Button.inline`), `events.CallbackQuery`, and `events.NewMessage`
with `ForceReply` — with **no new dependency** and a session pattern the team
already operates. B would add a dependency and a second mental model for the same
capability; its `ConversationHandler` is nice but our 3-step flow is small enough
that in-house state is simpler than mixing frameworks. C means reimplementing
update parsing/keyboards by hand for no benefit.

### Design decision — form UI mechanism

**Options considered:**
- **A. Inline buttons for game choice + `ForceReply` for the text prediction
  (recommended, adopted).**
- **B. Numbered text menu** ("reply 1–8 to choose a game").
- **C. Telegram Web App / custom keyboard form.**

**Recommendation: A.** Inline buttons give an unambiguous, tap-to-select game
choice (no parsing of "which game did they mean"), and the **prediction type**
(`Total`/`Spread`/`Moneyline`/`Other`) is a second inline-button row — the native
stand-in for the dropdown in the mockup — after which `ForceReply` opens a reply
box we can positively match via `reply_to`, capturing exactly the prediction and
not unrelated DMs. B is brittle (users mistype, indexes drift if the list changes
between prompt and reply). C (Web App) is the only way to render a true
dropdown-plus-input side by side, but it needs hosting, a bot domain, and JS —
beyond a "text field to start" v1, though it's the natural upgrade path if the
form grows into many structured fields.

---

## Data model

### Source sheet(s) — READ (see "Information needed")
Two logical reads (may be one tab or two):
1. **Game list** for the target date — e.g. columns `game_date, away_team,
   home_team, game_time` (this is exactly the shape
   `line-movement/sheets_utils.py::get_schedule_for_date` already returns).
2. **Game info** — `spread`, `over_under`/`total` for the selected game (may live
   in the same schedule row, or a separate odds/lines tab keyed by game).

### Intake sheet — WRITE (new, dedicated)
Proposed columns (append-only):

| Col | Field | Source |
|---|---|---|
| A | `submitted_at` | server timestamp (ET) |
| B | `telegram_user_id` | from `event.sender_id` |
| C | `telegram_username` | from sender entity |
| D | `sport` | command (`wnba`) |
| E | `game_date` | selected game |
| F | `away_team` | selected game |
| G | `home_team` | selected game |
| H | `game_time` | selected game |
| I | `spread` | game info sheet |
| J | `total` | game info sheet |
| K | `prediction_type` | dropdown selection (`Total` / `Spread` / `Moneyline` / `Other`) |
| L | `prediction_text` | free-text input (value + reason) |

Header-based lookup (like line-movement) so column reordering is safe.

---

## Design decision — Google auth

There are **two different Google auth conventions** already in play:

- **A. line-movement pattern (recommended, adopted):** base64-encoded
  `GOOGLE_CREDENTIALS` env var, scopes `spreadsheets` + `drive`, with
  `sheets_read`/`sheets_write` cooldown + 429-retry helpers
  (`line-movement/sheets_utils.py`).
- **B. forwarder pattern:** `GOOGLE_SERVICE_ACCOUNT_JSON` (path to a
  service-account JSON file), scope `spreadsheets` only
  (`telegram-channel-forwarder/sheets.py`).

**Recommendation: A.** We're porting the read/write logic from line-movement, so
taking its **battle-tested 429 handling and separate read/write rate-limit
buckets** with it avoids re-deriving that behavior — and intake does both reads
(game list/info) and writes (submission) that could otherwise collide with the
grader's Sheets quota. Keep it **self-contained in `intake_sheets.py`** (copy the
helpers) so the existing `sheets.py` used by the grader is untouched. Add
`GOOGLE_CREDENTIALS` to `.env` alongside the current config.

**Why not B:** it lacks the cooldown/backoff, and env-var credentials (base64)
are easier to sync across machines than a file path that must exist on each host.
**Trade-off accepted:** a second credential form lives in `.env`. If you'd prefer
a single credential, the fallback is to reuse `GOOGLE_SERVICE_ACCOUNT_JSON` and
port **only** the `sheets_read`/`sheets_write` cooldown logic onto it (still gets
the rate-limit safety, one credential). Captured in "Open decisions".

---

## Conversation state

In-memory dict keyed by `telegram_user_id`:
`{ user_id: {"stage": "awaiting_game"|"awaiting_type"|"awaiting_prediction", "sport": ..., "game": {...}, "info": {...}, "prediction_type": ..., "prompt_msg_id": ...} }`.

- Set on `/intake-wnba`, advanced on game selection → type selection → text
  reply, cleared on submit/cancel.
- Guard: match the reply via `event.message.reply_to` pointing at the bot's
  ForceReply prompt (`prompt_msg_id`) so we don't capture unrelated DMs.

### Design decision — state storage

**Options considered:**
- **A. In-memory dict (recommended for v1, adopted).** Ephemeral per-process
  state.
- **B. SQLite table** (`intake_sessions`) in the existing DB that `listener.py`
  already uses (it hosts `reply_chains`).
- **C. Stateless** — encode the whole selected game + info into the callback data
  / prompt so no server state is needed.

**Recommendation: A for v1.** The flow is a few seconds long and a restart is
rare; if state is lost the user simply re-runs the command — cheap and obvious.
This is the least code. **Why not B (yet):** durability isn't worth a schema and
migration for a transient 3-step flow, but it's the clear upgrade if we later want
sessions to survive restarts (add an `intake_sessions` table next to
`reply_chains`). **Why not C:** Telegram callback data is capped at 64 bytes, too
small to carry a game blob + spread/total reliably, so we'd still need a lookup —
defeating the point. **Trade-off accepted:** in-flight forms are dropped on
restart.

---

## Allowlist

Only an allowlist of Telegram user IDs may use the command (user-selected).

- Env var `INTAKE_ALLOWED_USER_IDS` — comma-separated numeric IDs in `.env`.
- Every handler (command, callback, reply) checks membership first; non-allowed
  users get a short "not authorized" reply and are ignored otherwise.

## Design decision — bot identity

**Options considered:**
- **A. Reuse the existing `BOT_TOKEN` / `BOT_SESSION` (recommended, adopted).**
- **B. Register a dedicated intake bot** (new token + session).

**Recommendation: A.** No new BotFather setup, no extra session to generate/rotate
(`scripts/get_bot_session.py` already produces `BOT_SESSION`), and users interact
with the same known bot. Because the intake bot is a **separate process**, running
two clients on the same token is fine as long as only one long-polls DMs — the
forwarder bot sends to channels, and the intake bot handles command DMs, so their
update scopes don't collide in practice.

**Why consider B:** cleaner separation of concerns and independent rate-limit
budget; if the forwarder bot and intake bot ever contend for `getUpdates` on the
same DM chat, a dedicated token removes any ambiguity. **Trade-off of A:** shared
token means a token rotation affects both. If DM update contention shows up in
testing, switch to B. Captured in "Open decisions".

---

## Steps (each independently landable)

### Step 0 — Confirm inputs (blocked on "Information needed")
Gather sheet IDs/tab names and column headers for the game list and game-info
reads, and create the new intake sheet shared with the service account. Nothing
to code until the source shape is known.

### Step 1 — `intake_sheets.py` (read + write, no bot)
- Port `get_gspread_client`, `sheets_read`, `sheets_write` (cooldown + 429) from
  `line-movement/sheets_utils.py`.
- `list_games(sport, date) -> list[dict]` — reads the game-list sheet
  (adapts `get_schedule_for_date`).
- `get_game_info(sport, game) -> dict` — reads spread/total for the game.
- `append_submission(row: dict) -> None` — header-based append to intake sheet.
- **Verify:** a throwaway `python -c` / script call lists today's games and
  appends a test row locally (in a venv).

### Step 2 — `intake_bot.py` (bot skeleton + allowlist)
- Telethon `TelegramClient(StringSession(BOT_SESSION), API_ID, API_HASH)` started
  with `bot_token=BOT_TOKEN` (same as `listener.py`).
- `load_dotenv()` + `.env.local` override (repo convention).
- Register `events.NewMessage(pattern=r'^/intake-wnba')`, allowlist gate, reply
  with inline keyboard from `list_games`.
- **Verify:** `python intake_bot.py` locally; `/intake-wnba` returns the game
  list; non-allowlisted user is refused.

### Step 3 — Game selection + type selection + info display
- `events.CallbackQuery` handler (game): decode selected game, call
  `get_game_info`, display spread/total/ML, and present the **type dropdown** as
  an inline-button row `[ Total ][ Spread ][ Moneyline ][ Other ]`; set state to
  `awaiting_type`.
- `events.CallbackQuery` handler (type): store `prediction_type`, send a
  `ForceReply` prompt ("<Type> — reply with your prediction + reason"), store
  `prompt_msg_id`, set state to `awaiting_prediction`.
- Callback data must be compact (Telegram 64-byte limit) — use short game
  index/key + type token into the state, not the full game blob.
- **Verify:** tapping a game shows correct spread/total/ML and the type row;
  tapping a type opens a reply box labeled with that type.

### Step 4 — Capture prediction + write row
- `events.NewMessage` (incoming, is-reply) handler: match `reply_to ==
  prompt_msg_id`, take `prediction_type` from state (the dropdown selection) and
  the reply body as `prediction_text`, build the row, `append_submission`,
  confirm to user, clear state.
- **Verify:** submitting writes a correct row to the intake sheet; confirmation
  echoes game · type · prediction.

### Step 5 — Deploy artifacts
- `run_intake_bot.sh` (mirror `run_grade_daemon.sh`).
- `deploy/systemd/telegram-intake.service` (mirror `grade-daemon.service`:
  `User=forwarder`, `EnvironmentFile=.env` + `-.env.local`, `Restart=on-failure`).
  No `WatchdogSec` needed for v1 (add later if it can wedge).
- Add the unit to `scripts/check_deploy_sync.sh` coverage.
- **Verify:** `bash scripts/check_deploy_sync.sh` clean; service starts on VPS,
  survives a restart, still handles `/intake-wnba`.

### Step 6 — Docs
- Add an "Intake bot" section to `CLAUDE.md` (service name, env vars, allowlist,
  sheet IDs, manual run command, test-mode notes).
- Update `requirements.txt` only if a new dep is truly needed (none expected).

---

## Extensibility (design for it, don't build yet)

- `/intake-wnba` is the first of a family (`/intake-nba`, `/intake-nhl`, …).
  Keep sport-specific config (source sheet id/tab, allowed prediction prefixes)
  in a small `INTAKE_SPORTS` dict/JSON in `.env` so new sports are config-only.
- Prediction is free text now; a future version can validate/parse the
  `Total:`/`Spread:` payload into structured fields.

---

## Testing / validation

- All Python work in a **venv** (never global).
- Local dry run: use a **test intake sheet** and the local bot session before
  touching the VPS. Follow the repo's cautious deploy rule — verify locally, then
  push + deploy only when confident (per `CLAUDE.md`).
- Watch for Telegram flood-waits on repeated bot restarts during dev.

---

## Files changed / added

| File | Change | Notes |
|---|---|---|
| `intake_sheets.py` | New | Sheets read/write, ports line-movement helpers |
| `intake_bot.py` | New | Telethon bot: command, callback, reply, allowlist |
| `run_intake_bot.sh` | New | venv launcher |
| `deploy/systemd/telegram-intake.service` | New | systemd unit |
| `scripts/check_deploy_sync.sh` | Modified | include new unit |
| `CLAUDE.md` | Modified | document the intake bot |
| `.env` / `.env.local` | Modified | new env vars (below) |

New env vars:
- `INTAKE_ALLOWED_USER_IDS` — comma-separated numeric Telegram user IDs
- `INTAKE_SHEET_ID` — new dedicated intake sheet (and `:gid`/tab as needed)
- `INTAKE_WNBA_SOURCE_SHEET` — source sheet id/tab for game list + info
- `GOOGLE_CREDENTIALS` — base64 service-account JSON (if adopting line-movement auth)

---

## Open decisions

1. **Auth form:** adopt line-movement's base64 `GOOGLE_CREDENTIALS` (recommended)
   vs. reuse the forwarder's `GOOGLE_SERVICE_ACCOUNT_JSON` path.
2. **State durability:** in-memory (v1) vs. SQLite table (`intake_sessions`).
3. **Same bot vs. new bot token:** reuse existing `BOT_TOKEN`/`BOT_SESSION`
   (simplest) vs. a dedicated intake bot token (cleaner separation, another
   session to manage).

---

## Information needed (please provide before Step 1)

These are the concrete inputs required — none are decided yet:

1. **Game list source** — Google Sheet **ID**, **tab name**, and **column
   headers** for the list of available WNBA games (need at least: date field,
   away team, home team, game time). Is it the `line-movement` `wnba_schedule`
   tab, or a different sheet?
2. **Game info source** — Sheet **ID** + **tab name** + **column headers** for
   **spread** and **total/over_under** per game. Same tab as #1, or separate?
   How is a game keyed (e.g. by `game_date` + team names)?
3. **Intake (output) sheet** — the new dedicated sheet's **ID** and **tab name**,
   and confirmation it's **shared with the service account** email.
4. **Service account** — which credential to use (existing
   `GOOGLE_SERVICE_ACCOUNT_JSON`, or provide base64 `GOOGLE_CREDENTIALS`), and the
   service-account email to share sheets with.
5. **Allowlist** — the list of Telegram **user IDs** permitted to run
   `/intake-wnba`.
6. **Bot** — confirm reuse of the existing `BOT_TOKEN`/`BOT_SESSION`, or provide a
   new bot token for a dedicated intake bot.
7. **Date scope** — should `/intake-wnba` show **today's** games (ET), or a date
   argument like `/intake-wnba 2026-07-17`? Default assumed: today (ET).
8. **Prediction types** — confirm the dropdown set is exactly `Total`, `Spread`,
   `Moneyline`, `Other` (add/remove any), and whether `Other` requires the user to
   name the market in the text or is free-form.
9. **Game info fields** — the mockup shows Spread, Total, and Moneyline (ML). Is
   ML available in the source sheet, or only spread/total for now?
