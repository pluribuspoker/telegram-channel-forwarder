# Plan: celebrity NFL win-total guesses

Feature request: add win-total guesses **on behalf of a celebrity** to the NFL
intake bot. A new button starts by asking for the celebrity, then the normal
team → wins flow proceeds attributed to that celebrity. It is **sticky** — once
a celebrity is chosen it stays active for multiple teams until the user taps the
button again or switches back to themselves.

Branch: `feature/celebrity-win-guesses` (implementation completed here).

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

## DONE
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
- Game-pick celebrity selection calls `get_or_create_celebrity`, so the
  registry stays the complete single source.
- `scripts/test_intake_bot.py`: added
  `test_celebrity_user_id_is_stable_negative_and_distinct`.
- The win-total UI now has the `🎤 Guess as a celebrity` entry point, a shared
  roster picker, free-text creation, sticky celebrity context, and a switch back
  to the sender's own guesses.
- Win browser, team detail, confirmation, and save progress all render the
  active `🎤` context and query progress under the effective celebrity id.
- `build_win_prediction_row` stamps celebrity rows with the negative id, name,
  and blank username without changing the prediction sheet schema.
- Save callbacks carry the effective identity and reject stale confirmations if
  the active guesser changed.
- Picker callbacks carry the deterministic celebrity id rather than a roster
  index, so an old button cannot select a different person after reordering.
- Win state is isolated from game-guess state so the two callback flows cannot
  corrupt each other.
- First roster load creates and seeds the registry from existing game-pick
  celebrity names.
- `nfl_win_predictions_latest` prefixes celebrity display names with `🎤`.
- Offline tests cover the picker, celebrity-aware views and rows, stable ids,
  registry ordering, latest-sheet marker, and nonnumeric legacy user ids.
- The NFL game/spread browser now uses the same beginning-of-flow celebrity
  picker. The chosen identity is shown throughout the game flow, automatically
  associated when the lean is saved, and remains sticky for subsequent games
  until the user switches back or starts a fresh command. This replaces the old
  post-save multi-celebrity picker.

## Testing
Completed locally with Python 3.12:
```
python -m pytest scripts/test_intake_bot.py scripts/test_nfl_win_predictions.py -q
# 40 passed
```

The interactive Telegram pass is still required before deploy. On the VPS as
`forwarder`:
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
