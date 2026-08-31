# Plan: celebrity NFL win-total guesses

Feature request: add win-total guesses **on behalf of a celebrity** to the NFL
intake bot. A new button starts by asking for the celebrity, then the normal
team → wins flow proceeds attributed to that celebrity. It is **sticky** — once
a celebrity is chosen it stays active for multiple teams until the user taps the
button again or switches back to themselves.

Branch: `feature/celebrity-win-guesses` (foundation already committed here).

## Confirmed decisions (from the requester)
- Stable synthetic `user_id` derived from the celebrity name **string**.
- `🎤` is the marker/separator wherever standings/entries are shown.
- Name entry is **free text**, plus buttons to pick a previously-entered celeb.
- Same team → wins flow after the celebrity is chosen.
- **Sticky**: keep making picks for the same celeb without re-choosing; only ask
  again when the button is tapped again (or "switch back to me").
- **Anyone** can use the new button.
- **One Google Sheets tab** is the single source of truth for celebrities we
  track (name + user_id + provenance), shared by all celebrity features. Fully
  shared list is fine for now.

## Design (and one refinement)
- New `celebrities` registry tab is the single source of truth.
- Each celebrity gets a **negative** synthetic id (`celebrity_user_id()`), so it
  can never collide with a real (positive) Telegram user id — and `user_id < 0`
  is the discriminator for rendering the `🎤`.
- **No schema change to `nfl_win_predictions`**: a celeb's win-total guesses are
  stored under the celeb's synthetic id using the EXISTING columns
  (`telegram_user_id`=celeb id, `telegram_display_name`=celeb name,
  `telegram_username`=""). Dedup + the `nfl_win_predictions_latest` tab already
  key on `telegram_user_id`, so a celeb accumulates its own per-team rows,
  separate from the submitter and from other celebs.
- **Provenance** (who first added a celeb) lives on the registry row
  (`created_by_user_id`/`created_by_username`), NOT on every guess. This is why
  no `submitted_by` column was added to `nfl_win_predictions` — it avoids a risky
  live-sheet schema migration (gspread `get_all_records(expected_headers=...)`
  would break if the header row changed under it).

## DONE (committed on this branch, syntax-checked + id-logic unit test)
In `intake_bot.py`:
- `CELEBRITY_REGISTRY_TAB` = `"celebrities"`, `CELEBRITY_REGISTRY_HEADERS`
  (`celebrity_id, celebrity_name, normalized_name, created_at_utc,
  created_at_et, created_by_user_id, created_by_username`).
- `_CELEBRITY_REGISTRY_LOCK`.
- `celebrity_user_id(name)` — deterministic negative id from the normalized,
  case-folded name.
- `_seed_registry_from_game_picks()` — one-time backfill of existing game-pick
  celeb names when the registry tab is first created.
- `_celebrity_registry_worksheet()` — create-with-headers (+ seed) / validate.
- `get_or_create_celebrity(name, *, created_by_user_id, created_by_username)` —
  idempotent on the normalized name; returns `{celebrity_id, celebrity_name}`.
- `load_celebrity_roster()` — **now reads the registry** (unified roster across
  features), most-recently-added first.
- Game-pick `celeb:save` handler now also calls `get_or_create_celebrity` for
  each saved name, so the registry stays the complete single source.
- `scripts/test_intake_bot.py`: added
  `test_celebrity_user_id_is_stable_negative_and_distinct`.

## TODO — the win-totals celebrity UI (not yet written)
Keep the repo's pattern: **logic in pure functions with offline unit tests**;
handlers just call them.

1. **build_win_prediction_row** — add optional `celebrity_id: int | None` and
   `celebrity_name: str | None`. When present: `user_id=celebrity_id`,
   `username=""`, `display_name=celebrity_name` (ignore first/last). Add an
   offline test asserting the row is stamped with the celeb id/name.
2. **Active-celebrity state** — store on the per-user `guess_states[sender_id]`
   dict, e.g. `state["win_celeb"] = {"id": <int>, "name": <str>}`. Sticky:
   set on pick/new, cleared on "switch back to me" or a fresh
   `/predict_nfl_wins`.
3. **Pure render fns:**
   - `win_celebrity_picker(roster: list[str]) -> (text, buttons)`: roster
     buttons `celebwin:pick:<index>`, a `➕ New celebrity` (`celebwin:new`), and
     a cancel/back. Header uses `🎤`.
   - Extend `win_prediction_browser(...)` to accept an optional
     `celebrity_name`: when set, show a `🎤 <name>` banner + a
     `↩ Back to my guesses` button (`celebwin:self`); when unset, add a
     `🎤 Guess as a celebrity` button (`celebwin:start`).
   - Make sure `win_prediction_team_detail` / confirmation / progress render the
     celeb context when active (the `🎤`).
4. **Handlers** (in the CallbackQuery dispatcher, alongside the `wins:*` block):
   - `celebwin:start` → `load_celebrity_roster()` → show `win_celebrity_picker`.
   - `celebwin:pick:<i>` → `get_or_create_celebrity(roster[i], created_by=...)`
     → set `state["win_celeb"]` → re-render browser as that celeb.
   - `celebwin:new` → `Button.force_reply` prompt; capture the reply in
     `capture_free_text` (guard on a stored `win_celeb_prompt_msg_id`), then
     `get_or_create_celebrity(text, created_by=...)` → set state → render browser.
   - `celebwin:self` → clear `state["win_celeb"]` → render browser as self.
   - In `wins:teams` / `winteam:` / `winpick:` / `winsave:`: compute an
     **effective identity** at the top — if `state["win_celeb"]` is set use the
     celeb id/name (and pass `celebrity_id/celebrity_name` into
     `build_win_prediction_row`), else the sender. Progress
     (`latest_predictions_for_user`) must use the effective id too.
5. **Standings / anywhere a name is shown**: prefix `🎤 ` when `user_id < 0`
   (celeb) so celebs are visually separated. `build_latest_prediction_rows`
   already keys per user+team, so celebs get their own rows automatically.
6. **New button placement**: an inline `🎤 Guess as a celebrity` button in the
   `/predict_nfl_wins` browser is the natural spot ("starts by asking for the
   celebrity"). (Optionally also a `/predict_celeb_wins` command — not required.)

## Testing (REQUIRED before deploy — could not be done from the authoring env)
The authoring session had no `telethon`/`pytest` and no access to the live venv,
so only `py_compile` + the pure id test were run. On the VPS as `forwarder`:
```
cd ~/app && ~/venv/bin/python -m pytest scripts/test_intake_bot.py -q
# then a live test-mode pass and DM the bot to walk the flow:
~/venv/bin/python listener.py --test
```
Verify: pick a celeb (new + from roster), make several team guesses (sticky),
switch back to self, confirm celeb rows land in `nfl_win_predictions` under the
negative id, the `celebrities` tab has the row, and standings show `🎤`.

## Deploy (only after tests pass)
Per workspace rules for this repo: run its tests, commit + push a clean `main`,
then `request-telegram-intake-deploy --summary "<what changed>"` (binds the
pushed revision, fast-forwards `/home/forwarder/app`, restarts
`telegram-intake.service` once). Do NOT SSH + `git pull` + restart by hand.

## Gotchas
- The registry auto-seeds from existing game-pick celeb names the first time the
  `celebrities` tab is created on the live sheet — expected, not a surprise.
- `load_celebrity_roster` now depends on the registry; the game-pick save path
  was updated to register names so nothing is lost, but if you add another entry
  point that creates celeb names, route it through `get_or_create_celebrity`.
- Keep celeb ids negative; `user_id < 0` is the discriminator used for `🎤` and
  for keeping celeb rows out of a real user's standings.
