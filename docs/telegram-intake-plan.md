# NFL Telegram Lean Intake → Google Sheets — Plan

## Goal

Add a Telegram-based NFL lean intake system. A user DMs a dedicated intake bot
with `/intake-nfl`, the bot presents the available NFL games (read live from a
Google Sheet), and the user taps a game. The bot shows first-observed and latest
BetOnline spread, moneyline, and total prices from The Odds API, then requires the user to select a market and side
before entering a free-text opinion, including any line or price at which their
preference would change. The submission is appended to a **new dedicated intake
Google Sheet/tab**.

Reuses the Google Sheets access patterns proven in the `line-movement/` repo
(service-account auth + read/write rate-limit buckets + 429 backoff).

## Living-plan rule

This file is the authoritative implementation record. As work progresses, record
material decisions here with their rationale and status before or alongside the
code change. If implementation differs from an earlier proposal, update the
proposal rather than leaving contradictory guidance in place.

## Implementation status

### Completed — 2026-08-04: NFL line data foundation

- Created a new Google workbook dedicated to this project; no sheets from
  `line-movement` are reused.
- Created `nfl_games`, `nfl_line_snapshots`, and `nfl_leans` tabs with structured
  headers.
- Configured a dedicated Google service account through base64
  `GOOGLE_CREDENTIALS`; the workbook ID is configured through
  `NFL_INTAKE_SHEET_ID`.
- Added `nfl_lines.py` and `scripts/fetch_nfl_lines.py`.
- Added focused coverage in `scripts/test_nfl_lines.py`.
- Completed a live BetOnline fetch and real Sheet write. The immediate repeat
  updated game freshness without appending duplicate snapshots.

### Next

- Deploy and enable the completed VPS systemd service/timer.
- Build the dedicated Telegram bot only after the recurring data source is
  running reliably.

### In progress — 2026-08-04: VPS collector deployment

Deployment checklist:

1. Commit and push the fetcher, tests, runner, timer, and living-plan updates.
2. Sync `ODDS_API_KEY`, `GOOGLE_CREDENTIALS`, and `NFL_INTAKE_SHEET_ID` to the
   VPS through the repository's existing environment workflow.
3. Pull the commit on the VPS.
4. Install `nfl-lines-fetcher.service` and `nfl-lines-fetcher.timer`.
5. Enable the timer and trigger one manual service run.
6. Confirm service logs, timer state, no-op cadence behavior, and workbook
   integrity.

Issues and deviations encountered during this phase will be recorded below
before the phase is marked complete.

Deployment issue log:

- **Stale local clone:** the local checkout was 225 commits behind
  `origin/main`, while the VPS already matched the current remote head. The
  collector work was stashed, `main` was fast-forwarded to `b2ed682`, and the
  work was reapplied without conflicts before committing.

---

## User flow (end-to-end experience)

The walkthrough below is an NFL example: the user asks for the current slate, the
bot lists the games, the user selects a game, and the bot shows opening and latest
prices. The user then selects a market and side before submitting a lean,
rationale, and any price-dependent conditions.

```
 ┌──────────────────────────────────────────────────────────────┐
 │  Chat with  @IntakeBot                                        │
 ├──────────────────────────────────────────────────────────────┤
 │                                                              │
 │                                    ┌───────────────────────┐  │
 │                                    │  /intake-nfl          │  │  ← user
 │                                    └───────────────────────┘  │
 │                                                              │
 │  ┌────────────────────────────────────────────────┐          │
 │  │ 🏈  NFL — available games                        │          │  ← bot
 │  │ Which game do you want to predict?              │          │
 │  │                                                 │          │
 │  │  ┌───────────────────────────────────────────┐ │          │
 │  │  │  Dolphins @ Bills      · Sun 1:00 PM ET   │ │  ◄ tap    │
 │  │  ├───────────────────────────────────────────┤ │          │
 │  │  │  Ravens @ Bengals      · Sun 1:00 PM ET   │ │          │
 │  │  ├───────────────────────────────────────────┤ │          │
 │  │  │  Packers @ Bears       · Sun 4:25 PM ET   │ │          │
 │  │  ├───────────────────────────────────────────┤ │          │
 │  │  │  Cowboys @ Eagles      · Sun 8:20 PM ET   │ │          │
 │  │  └───────────────────────────────────────────┘ │          │
 │  │        (inline keyboard — one button per game)  │          │
 │  └────────────────────────────────────────────────┘          │
 │                                                              │
 │                                    ┌───────────────────────┐  │
 │                                    │ (taps “Dolphins @ Bills”)│  ← user
 │                                    └───────────────────────┘  │
 │                                                              │
 │  ┌────────────────────────────────────────────────┐          │
 │  │ 📋  Dolphins @ Bills — Sun 1:00 PM ET           │          │  ← bot
 │  │ ─────────────────────────────────────────────   │          │
 │  │   Spread :  Open BUF -3.5 · Latest BUF -2.5     │          │
 │  │   Total  :  Open 47.5 · Latest 46.5             │          │
 │  │   ML     :  Open MIA +155 · Latest MIA +130     │          │
 │  │ ─────────────────────────────────────────────   │          │
 │  │ Enter your prediction:                          │          │
 │  │                                                 │          │
 │  │   Market ▾          Your lean                    │          │
 │  │  ┌───────────────┐ ┌───────────────────────────┐│          │
 │  │  │ Spread      ▾ │ │ Dolphins +2.5. Prefer +3  ││  ◄ type   │
 │  │  │───────────────│ │ or better; ML at +140.    ││    +      │
 │  │  │ Spread        │ └───────────────────────────┘│    input  │
 │  │  │ Moneyline     │        [ Submit ]             │          │
 │  │  │ Total         │                               │          │
 │  │  │ Other         │                               │          │
 │  │  └───────────────┘                               │          │
 │  └────────────────────────────────────────────────┘          │
 │                                                              │
 │                                    ┌───────────────────────┐  │
 │                                    │ Market: Spread        │  │  ← user
 │                                    │ Side: Miami Dolphins  │  │
 │                                    │ Prefer +3 or better…  │  │
 │                                    └───────────────────────┘  │
 │                                                              │
 │  ┌────────────────────────────────────────────────┐          │
 │  │ ✅  Logged your prediction                       │          │  ← bot
 │  │   Dolphins @ Bills · Spread · Miami Dolphins    │          │
 │  │   “Prefer +3 or better; ML at +140”             │          │
 │  │   → saved to intake sheet                       │          │
 │  └────────────────────────────────────────────────┘          │
 │                                                              │
 └──────────────────────────────────────────────────────────────┘
```

### How the “dropdown + input box” maps to Telegram

Telegram DMs have **no native side-by-side dropdown-with-textbox widget**. The
initial implementation will deliberately prototype the closest native experience
before deciding whether a hosted form is justified:

- **Native (v1, adopted):** inline buttons select the market and side; tapping
  them opens a `ForceReply` input box where the user enters their rationale and
  any line/price conditions. This prototype will use realistic NFL line movement
  so the team can evaluate the experience before adding hosting.
- **Telegram Web App (later):** a real HTML form renders the dropdown and text box
  exactly as drawn, submitting both fields at once. Requires hosting + a bot
  domain; natural upgrade if the form grows.

Text-transcript form of the same flow:

```
User: /intake-nfl
Bot:  "NFL — available games:"     [inline keyboard, one button per game]
        [ Dolphins @ Bills · Sun 1:00 PM ] ...
User: (taps "Dolphins @ Bills")
Bot:  "Dolphins @ Bills — Sun 1:00 PM ET
       Spread: Open BUF -3.5 | Latest BUF -2.5
       Total:  Open 47.5     | Latest 46.5
       ML:     Open MIA +155 | Latest MIA +130
       Choose a market:"  [ Spread ][ Moneyline ][ Total ][ Other ]
User: (taps "Spread")
Bot:  "Choose a side:"             [ Miami Dolphins ][ Buffalo Bills ]
User: (taps "Miami Dolphins")
Bot:  "Miami Dolphins spread — enter your lean, reasoning, and the line or
       price at which your preference changes:"                 [ForceReply]
User: (replies) "Dolphins +2.5. Prefer +3 or better; ML instead at +140."
Bot:  "✅ Logged. Dolphins @ Bills · Spread · Miami Dolphins …"
      → row appended to the intake sheet
```

---

## Architecture

```
intake_bot.py            ← new: dedicated Telethon bot, command/callback/reply handlers,
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
- **A. Separate process/systemd service (adopted).** A standalone, dedicated
  Bot-API-only intake bot, like `grade_daemon.py`.
- **B. Add handlers to the existing `listener.py` bot.** Reuse the already-running
  bot client and event loop.
- **C. Extend the existing Telegram Channels Claude bot** (the tmux `claude`
  session) to handle intake.

**Decision: A.** The forwarder `listener.py` runs a persistent Telethon
**user** session and is flood-wait sensitive (`CLAUDE.md`: "Deploy cautiously.
Rapid bot session restarts trigger Telegram flood waits"). Adding stateful,
frequently-iterated command handlers there means every intake code change forces
a listener restart, risking flood-waits and forwarding downtime. Option C couples
intake to an interactive AI session that has no history/backfill (dropped-message
risk noted in `CLAUDE.md`) and isn't a deterministic form handler. Option A keeps
intake **Bot-API-only** (zero Telethon-user/session risk, exactly the isolation
rationale behind `grade_daemon.py`), independently deployable and restartable, and
a crash/flood-wait on either side can't take down the other.

**Trade-off accepted:** one more service, bot token, session, and identity to
manage. This avoids update-polling ambiguity and allows a custom name and profile
picture without coupling intake to the forwarding bot.

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

**Decision: A for the prototype.** Inline buttons give an unambiguous, tap-to-select game
choice (no parsing of "which game did they mean"), and the **prediction type**
(`Total`/`Spread`/`Moneyline`/`Other`) is a second inline-button row — the native
stand-in for the dropdown in the mockup — after which `ForceReply` opens a reply
box we can positively match via `reply_to`, capturing exactly the prediction and
not unrelated DMs. B is brittle (users mistype, indexes drift if the list changes
between prompt and reply). C (Web App) is the only way to render a true
dropdown-plus-input side by side, but it needs hosting, a bot domain, and JS —
beyond the native prototype. After the prototype is used with realistic NFL
examples, the team will decide whether the improved UX justifies hosting.

---

## Data model

### `nfl_games` — current bot-facing state

One row per Odds API `event_id`, updated in place. The tab has 20 columns:

- Event metadata: event ID, season/type/week/status, UTC/ET kickoff, teams, and
  bookmaker.
- `opening_captured_at` and `latest_captured_at`.
- Three packed opening columns: away, home, totals.
- Three packed latest columns: away, home, totals.
- `last_updated_at` and `period_last_checked_at`.

The six market columns use the same positional format as the snapshot tab. This
keeps bot reads small while retaining all full-game, first-half, and
first-quarter markets.

### `nfl_line_snapshots` — append-only movement history

Append one row only when a market payload differs from the latest persisted
payload for that event. The tab has 12 columns: seven identity/time columns,
three packed market columns, and two API-quota columns.

Packed columns:

- `away_game_spread_spreadprice_moneyline__h1_spread_spreadprice_moneyline__q1_spread_spreadprice_moneyline`
- `home_game_spread_spreadprice_moneyline__h1_spread_spreadprice_moneyline__q1_spread_spreadprice_moneyline`
- `totals_game_total_overprice_underprice__h1_total_overprice_underprice__q1_total_overprice_underprice`

Encoding rules:

- `|` separates full game, first half, and first quarter.
- `,` separates the fields documented by the column name.
- `nodata` explicitly represents a missing value.

Example away value:

```text
3.5,-105,165|nodata,nodata,nodata|nodata,nodata,nodata
```

### `nfl_leans` — Telegram submissions

This remains append-only and will include submission/user/game metadata, the
selected period/market/side, target line or price, and free-text reasoning. Its
final line-context columns will be finalized before the bot writer is
implemented; do not assume the current placeholder header layout is final.

---

## Design decision — Google auth

There are currently **two different Google auth conventions** in the related
codebases:

- **A. line-movement pattern (adopted repo-wide):** base64-encoded
  `GOOGLE_CREDENTIALS` env var, scopes `spreadsheets` + `drive`, with
  `sheets_read`/`sheets_write` cooldown + 429-retry helpers
  (`line-movement/sheets_utils.py`).
- **B. forwarder pattern (to be retired):** `GOOGLE_SERVICE_ACCOUNT_JSON` (path to a
  service-account JSON file), scope `spreadsheets` only
  (`telegram-channel-forwarder/sheets.py`).

**Decision: A only.** Migrate the existing forwarder Sheets consumer and the new
intake service together so both decode `GOOGLE_CREDENTIALS` and share the proven
cooldown/backoff behavior. Remove `GOOGLE_SERVICE_ACCOUNT_JSON` after the
migration is deployed and verified; do not retain dual loaders or silent
fallbacks. The credential remains only in untracked environment configuration
and must never be committed.

---

## Conversation state

In-memory dict keyed by `telegram_user_id`:
`{ user_id: {"stage": ..., "sport": "nfl", "game": {...}, "lines": {...}, "market": ..., "side": ..., "prompt_msg_id": ...} }`.

- Set on `/intake-nfl`, advanced on game selection → market selection → side
  selection → text
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

**Decision: A for v1.** The flow is a few seconds long and a restart is
rare; if state is lost the user simply re-runs the command — cheap and obvious.
This is the least code. **Why not B (yet):** durability isn't worth a schema and
migration for a transient 3-step flow, but it's the clear upgrade if we later want
sessions to survive restarts (add an `intake_sessions` table next to
`reply_chains`). **Why not C:** Telegram callback data is capped at 64 bytes, too
small to carry a game blob + spread/total reliably, so we'd still need a lookup —
defeating the point. **Trade-off accepted:** in-flight forms are dropped on
restart.

For a future Telegram Web App, signed query parameters may carry a short-lived,
non-sensitive state identifier across restarts. The server must still validate
Telegram identity and load authoritative line snapshots server-side; raw prices
or trusted user data must not be accepted from the URL.

---

## Allowlist

Only an allowlist of Telegram user IDs may use the command (user-selected).

- Env var `INTAKE_ALLOWED_USER_IDS` — comma-separated numeric IDs in `.env`.
- Every handler (command, callback, reply) checks membership first; non-allowed
  users get a short "not authorized" reply and are ignored otherwise.

## Design decision — bot identity

**Options considered:**
Use a **dedicated intake bot** with its own token, session, name, and profile.
This is a resolved requirement, not an open decision. It prevents update-polling
conflicts, isolates failures and token rotation, and gives the intake experience
room for its own identity.

---

## Steps (each independently landable)

### Step 0 — Data-source setup (completed)
The dedicated workbook, service account, BetOnline Odds API source, tab schemas,
and environment variables are configured. Dedicated bot registration and the
allowlist remain intentionally deferred until the data pipeline is scheduled.

### Step 1 — `intake_sheets.py` (read + write, no bot)
- Port `get_gspread_client`, `sheets_read`, `sheets_write` (cooldown + 429) from
  `line-movement/sheets_utils.py`.
- `list_games(sport, date) -> list[dict]` — reads the game-list sheet
  (adapts `get_schedule_for_date`).
- `get_game_info(sport, game) -> dict` — reads opening/latest spread, moneyline,
  and total snapshots for the game.
- `append_submission(row: dict) -> None` — header-based append to intake sheet.
- **Verify:** a throwaway `python -c` / script call lists today's games and
  appends a test row locally (in a venv).

### Step 2 — `intake_bot.py` (bot skeleton + allowlist)
- Telethon `TelegramClient(StringSession(BOT_SESSION), API_ID, API_HASH)` started
  with `bot_token=BOT_TOKEN` (same as `listener.py`).
- `load_dotenv()` + `.env.local` override (repo convention).
- Register `events.NewMessage(pattern=r'^/intake-nfl')`, allowlist gate, reply
  with inline keyboard from `list_games`.
- **Verify:** `python intake_bot.py` locally; `/intake-nfl` returns the game
  list; non-allowlisted user is refused.

### Step 3 — Game, market, and side selection
- `events.CallbackQuery` handler (game): display opening/latest spread, total,
  and moneyline, then present `[ Spread ][ Moneyline ][ Total ][ Other ]`.
- Market handler: store the selected market and present valid sides (teams for
  spread/moneyline, over/under for total).
- Side handler: store the side and send a `ForceReply` asking for the lean,
  rationale, and any line/price at which the preference changes.
- Callback data must be compact (Telegram 64-byte limit) — use short game
  index/key + type token into the state, not the full game blob.
- **Verify:** tapping Dolphins @ Bills shows correct opening/latest lines;
  selecting Spread → Miami Dolphins opens the expected reply prompt.

### Step 4 — Capture prediction + write row
- `events.NewMessage` (incoming, is-reply) handler: match `reply_to ==
  prompt_msg_id`, combine the stored line snapshot, market, side, and reply body,
  append the structured row, confirm to the user, and clear state.
- **Verify:** submitting writes a correct row to the intake sheet; confirmation
  echoes game · type · prediction.

### Step 5 — Deploy artifacts
- `run_intake_bot.sh` (mirror `run_grade_daemon.sh`).
- `deploy/systemd/telegram-intake.service` (mirror `grade-daemon.service`:
  `User=forwarder`, `EnvironmentFile=.env` + `-.env.local`, `Restart=on-failure`).
  No `WatchdogSec` needed for v1 (add later if it can wedge).
- Add the unit to `scripts/check_deploy_sync.sh` coverage.
- **Verify:** `bash scripts/check_deploy_sync.sh` clean; service starts on VPS,
  survives a restart, still handles `/intake-nfl`.

### Step 6 — Docs
- Add an "Intake bot" section to `CLAUDE.md` (service name, env vars, allowlist,
  sheet IDs, manual run command, test-mode notes).
- Update `requirements.txt` only if a new dep is truly needed (none expected).

---

## Extensibility (design for it, don't build yet)

- `/intake-nfl` is the first of a family (`/intake-nba`, `/intake-nhl`, …).
  Keep sport-specific config (source sheet id/tab, allowed prediction prefixes)
  in a small `INTAKE_SPORTS` dict/JSON in `.env` so new sports are config-only.
- Market and side are structured now. The initial version keeps rationale and
  movement-dependent conditions as free text; a later version may parse
  `target_line_or_price` automatically or collect it through another control.

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
| `nfl_lines.py` | New | BetOnline fetch, normalization, opening/latest merge, Sheet persistence |
| `scripts/fetch_nfl_lines.py` | New | Dry-run and `--write` CLI |
| `scripts/test_nfl_lines.py` | New | Parsing, season, opening, row-index, and snapshot tests |
| `run_nfl_lines_fetcher.sh` | New | Scheduled-mode VPS launcher |
| `deploy/systemd/nfl-lines-fetcher.service` | New | One-shot line fetch service |
| `deploy/systemd/nfl-lines-fetcher.timer` | New | 30-minute cadence-check timer |
| `intake_sheets.py` | New | Sheets read/write, ports line-movement helpers |
| `intake_bot.py` | New | Telethon bot: command, callback, reply, allowlist |
| `run_intake_bot.sh` | New | venv launcher |
| `deploy/systemd/telegram-intake.service` | New | systemd unit |
| `scripts/check_deploy_sync.sh` | Modified | include new unit |
| `CLAUDE.md` | Modified | document the intake bot |
| `.env` / `.env.local` | Modified | dedicated bot and intake env vars |

New env vars:
- `INTAKE_ALLOWED_USER_IDS` — comma-separated numeric Telegram user IDs
- `NFL_INTAKE_SHEET_ID` — dedicated workbook containing all three intake tabs
- `INTAKE_BOT_TOKEN` / `INTAKE_BOT_SESSION` — dedicated intake bot credentials
- `GOOGLE_CREDENTIALS` — base64 service-account JSON, used repo-wide
- `ODDS_API_KEY` — The Odds API credential used for BetOnline lines

---

## Resolved decisions

1. Start with a native Telegram prototype; consider a Web App only after using
   the prototype with realistic NFL data.
2. Run a separate process with a dedicated intake bot.
3. Keep transient multi-step state in memory for v1.
4. Use line-movement's base64 `GOOGLE_CREDENTIALS` approach repo-wide.
5. Require structured market and side selection; store the opening/latest line
   snapshot and keep reasoning/price conditions as text initially.
6. Use the Miami Dolphins in all sample user selections and submitted opinions.
7. Use BetOnline (`betonlineag`) through The Odds API as the fixed bookmaker so
   movement is always an apples-to-apples comparison.

## Implementation decision log

### 2026-08-04 — Dedicated workbook

**Decision:** Create a new workbook for this intake system rather than reuse any
`line-movement` workbook or tab.

**Rationale:** The projects may reuse code patterns, but their operational data,
permissions, and lifecycle should remain independent.

**Workbook layout:**

- `nfl_games` — one current row per Odds API event, optimized for bot reads.
- `nfl_line_snapshots` — append-only line history.
- `nfl_leans` — append-only Telegram submissions.

There is no separate schedule-only tab. The Odds API event response supplies the
event ID, teams, and kickoff time together with the markets.

### 2026-08-04 — BetOnline as the fixed line source

**Decision:** Fetch `h2h`, `spreads`, and `totals` from BetOnline
(`betonlineag`) through The Odds API.

**Rationale:** Comparing the same bookmaker over time measures actual movement.
Selecting the best available book on each poll could create false movement when
the selected bookmaker changes.

Both active sport keys are collected:

- `americanfootball_nfl_preseason`
- `americanfootball_nfl`

### 2026-08-04 — Meaning of “opening”

**Decision:** “Opening” means the first valid value observed by this system for
each market, not necessarily BetOnline's true market-open price.

**Rationale:** The current Odds API poll supplies the latest price. If polling
begins after a market was posted, claiming that first captured value as the
book's original opener would be misleading. A market omitted in the first
response initializes its opening value when it first becomes available.

### 2026-08-04 — Current rows plus append-only history

**Decision:** Upsert every event into `nfl_games`, preserving opening fields and
updating latest fields. Append to `nfl_line_snapshots` only when any market
changes.

**Rationale:** The bot gets a small, fast current-state table while the snapshot
tab preserves movement history without adding unchanged hourly rows.

Manual blank rows in the workbook are tolerated: updates use physical Sheet row
numbers rather than positions in a filtered list.

### 2026-08-04 — Duplicate prevention

**Decision:** Every append path must perform an application-level duplicate
check before writing.

- `nfl_games` is unique by Odds API `event_id` and is updated in place.
- `nfl_line_snapshots` compares the candidate market payload with the latest
  persisted payload for that event and appends only when it differs.
- Multiple copies of an event returned or passed within one run are collapsed by
  `event_id`.
- `nfl_leans` will use a unique `submission_id`; retries must check that ID before
  appending.

A line returning to a previously seen value is **not** considered a duplicate
when an intervening value existed; that reversal is real movement and should be
recorded.

### 2026-08-04 — Full-game and period markets

**Decision:** Track spread, moneyline, and total for all three periods:

- Full game
- First half
- First quarter

Full-game markets come from the sport-level endpoint. First-half and first-quarter
markets require The Odds API's per-event endpoint, so the fetcher checks every
upcoming event on every run rather than limiting period checks to games within a
specific number of days.

**Rationale:** Period markets may be posted at different times, including well
before kickoff. Checking every event avoids missing their first observed value.
The live BetOnline probe returned no period markets for the currently available
games, but unavailable markets consumed no additional quota. Period fields remain
blank until BetOnline publishes them, then initialize their opening value from
the first valid observation.

The workbook schemas now include all three periods. `nfl_games` stores six packed
market columns (opening/latest × away/home/totals), while
`nfl_line_snapshots` stores three packed market columns (away/home/totals).
Missing values are written as the literal `nodata` so positions remain explicit
and survive CSV/Sheet transformations.

### 2026-08-04 — Google Sheets capacity

**Constraint:** Design against Google Sheets' standard **10 million cells per
spreadsheet** limit. The limit applies across every tab in the workbook, so
adding more tabs to the same spreadsheet does not increase capacity.

The packed format and change-only snapshot appends substantially reduce cell
growth, but they do not remove the long-term limit. Monitor workbook cell usage
before each season and establish an archival threshold well below 10 million.

When capacity becomes material, choose one of:

1. Archive completed seasons into additional **spreadsheet files** and keep the
   active season in the operational workbook.
2. Move line history to a database and retain Google Sheets only as a current
   view/export and human-facing intake surface.

A database is the preferred long-term destination if the history becomes large
or needs non-trivial querying.

### 2026-08-04 — Scheduling location

**Decision:** Run the recurring fetcher through a VPS systemd timer, not GitHub
Actions.

**Rationale:** Credentials already live on the VPS, systemd scheduling is more
predictable than scheduled Actions, and the repository already operates and
monitors recurring jobs this way.

The timer wakes every 30 minutes, but scheduled mode reads `nfl_games` first and
exits without an Odds API call when nothing is due. Adopted per-game bands:

| Time to kickoff | Poll interval |
|---|---:|
| More than 7 days | 24 hours |
| More than 24 hours through 7 days | 12 hours |
| More than 4 hours through 24 hours | 1 hour |
| 4 hours or less | 30 minutes |
| Started or past | Stop polling |

`last_updated_at` gates the sport-level full-game refresh.
`period_last_checked_at` independently gates first-half/first-quarter per-event
requests. Whenever a sport-level call is due, it returns all listed games, so
distant full-game lines may be refreshed more often at no additional request.
If a game's period request is not due, the writer preserves its last known H1/Q1
values rather than replacing them with `nodata` or recording false movement.

Implemented artifacts:

- `run_nfl_lines_fetcher.sh`
- `deploy/systemd/nfl-lines-fetcher.service`
- `deploy/systemd/nfl-lines-fetcher.timer`

---

## Remaining inputs

The data source, workbook, credentials, and schemas are resolved. Inputs still
needed before the Telegram bot is deployed:

1. **Allowlist** — the list of Telegram **user IDs** permitted to run
   `/intake-nfl`.
2. **Bot** — create the dedicated bot and provide its token/session plus desired
   display name and profile image.
3. **Slate scope** — decide whether `/intake-nfl` shows the current week, accepts
   a week/date argument, or offers both. Default recommendation: current week.
4. **Markets** — confirm the set is `Spread`, `Moneyline`, `Total`, `Other`, and
   define how `Other` should identify its market.
5. **Target condition structure** — confirm free text for v1, with an optional
   best-effort parsed `target_line_or_price`, rather than another required step.
