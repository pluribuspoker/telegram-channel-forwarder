# Telegram Channel Forwarder

## Preferences

- When giving shell commands, chain related steps with `&&` on one line rather than separate lines.
- Use git worktrees for parallel tasks (e.g. a second task while another Claude session is already working). Pattern: `git worktree add ../telegram-forwarder-<slug> -b <branch>`, work there, commit+push the branch, then merge to main from the main repo dir. Name the dir `../telegram-forwarder-<short-slug>`. Clean up with `git worktree remove ../telegram-forwarder-<slug>`.

## Telegram message formatting

When sending/forwarding messages, always preserve `msg.entities` (bold, italic, blockquotes, etc.) via `formatting_entities=`. Never rebuild message text without passing entities through. Use `text_suffix` in `send_group` to append text without dropping entities.

**`entities` pair with `raw_text`, never with `text`.** Telethon's `msg.text` is `parse_mode.unparse(raw_text, entities)` — the markdown *render*, with `**`/`__`/`~~`/backticks inserted as literal characters — while entity offsets index `raw_text`. Passing `formatting_entities=` also tells Telethon to **skip parsing**, so the mismatch is never re-derived: the delimiters stay in the message and every entity from the first one onward lands N chars early. It renders as a *partly* correct message, which is why it survives review — `**SATURDAY**` came out bold over `**SATURD` with a stray `AY**`, and the blockquote 4 chars downstream swallowed the wrong lines (fixed 33c3e31). Rule: **whatever string goes to Telegram beside `formatting_entities` must be `raw_text`.** Text sent *without* entities is the opposite case — `enrich_caption` keeps `.text` deliberately, because that caption is markdown-parsed on the way out.

The same `.text`/`.raw_text` confusion is silent everywhere else it appears: `filter_pattern` regexes, `reply_chain_cappers` prefixes and any other content matching run against `.text` see `**Tony POD**`, so an anchored `^Tony POD` stops matching and the pick is **dropped with no error anywhere**. Match on `raw_text`. `scripts/test_forward_entities_regression.py` pins all four send paths.

## VPS

- **Reserved IP:** `209.38.51.86` (always use this, not the droplet IP)
- **SSH:** `ssh root@209.38.51.86`
- **Aliases (interactive SSH only):** defined in `/root/.server_aliases.sh` — `flogs`, `tlogs`, `logs`, `start`, `stop`, `restart`, `status`, `deploy`, `grade`, `gradetest`. These are not available via non-interactive `ssh root@... 'command'`.

**Deploy cautiously.** Rapid bot session restarts trigger Telegram flood waits. If you are confident in a fix and have verified it, you may push and deploy. Otherwise let the user handle it.

### Switching to test mode

**Locally** (no stop/start needed — uses local `.env.local` sessions):
```bash
python listener.py --test
```

**On VPS** (must stop the live service first):
```bash
stop
su - forwarder
cd ~/app
~/venv/bin/python listener.py --test
# Ctrl+C when done
exit
start
```

In both cases, `test_source_channel` → `test_dest_channel` from `MAPPINGS_CONFIG`, and all `filter_pattern` checks are bypassed.

### Service names

- `telegram-forwarder.service` — listener (persistent). Aliases: `flogs` / `tlogs`.
- `telegram-tracker.timer` — pick grader, every 5 min. Scans Telegram for new picks, parses, fetches odds, applies cached verdicts.
- `grade-daemon.service` — grade daemon (persistent). Grades pending picks every 10s via ESPN + Claude, edits emoji + broadcasts via Bot API. **Zero Telethon** — no session/flood risk. Logs: `journalctl -u grade-daemon`. **Hang-hardened:** each cycle is capped at `CYCLE_TIMEOUT` (env `GRADE_DAEMON_CYCLE_TIMEOUT`, default 300s) and aborted+retried if exceeded; the daemon feeds a systemd `WatchdogSec=600` (sends `WATCHDOG=1` each loop) so a fully wedged process auto-restarts. Broadcasts persist to the cache immediately (not just end-of-cycle) so an abort/restart never double-posts.
- `angles-dashboard.service` — angle analyzer web dashboard (persistent). Serves `https://fightclubpicks.cc`. Env: `ANGLES_AUTH_SECRET`, `ANGLES_PORT`.
- `mem-watchdog.timer` — VPS memory monitor (`deploy/mem_watchdog.py`), every 10 min. Stays silent unless it DMs the operator via the watchdog bot: 🔴 on a kernel OOM-kill, 🟡 on sustained swap pressure (>1GB used ~40min+ → upgrade signal). Reuses `WATCHDOG_BOT_TOKEN`/`WATCHDOG_USER_ID`; state in `~/.mem_watchdog_state.json`. The VPS has a **2GB swapfile** (`/swapfile`, in `/etc/fstab`, `vm.swappiness=10`) — added because it previously had zero swap and OOM-killed processes under spikes.
- `claude-spend-watchdog.timer` — Claude API spend monitor (`deploy/claude_spend_watchdog.py`), hourly at :07. Silent unless it DMs the operator via the watchdog bot: 💸 when trailing-24h spend exceeds `CLAUDE_SPEND_DAY_ALERT_USD` (default $3), ⚡ when trailing-1h exceeds `CLAUDE_SPEND_HOUR_ALERT_USD` (default $0.60 — catches a fast leak before the daily figure moves). Reuses `WATCHDOG_BOT_TOKEN`/`WATCHDOG_USER_ID`; state in `~/.claude_spend_watchdog_state.json`; debounce 12h daily / 6h hourly. Sums the `[Claude] $` lines `ai.py` prints, per unit. Check the numbers by hand any time with `--report` (prints, sends nothing). **Added because spend had no signal at all:** one mis-parsed pick re-graded in a loop cost ~$9.40/day for five days before anyone thought to grep for it.
- `claude-watchdog-bot.service` — interactive watchdog bot (`deploy/claude_watchdog_bot.py`). Uses `WATCHDOG_BOT_TOKEN`. Menu commands: `/mem` (RAM/swap usage), `/status` (service status), `/restart`, `/kill` (force-kill+restart), `/logs` (last 20 journal lines), `/tmux` (Claude's current pane). Commands set via `setMyCommands` Bot API.

### Claude Code via Telegram (Channels)

Claude Code runs on VPS in a tmux session with the official Telegram channels plugin. The user DMs `@ForwarderClaudeBot` on Telegram to interact with Claude Code — full CLI features (skills, hooks, memory, dangerous mode) work.

- **tmux session:** `tmux attach -t claude` (as forwarder user)
- **Restart:** `su - forwarder -c "tmux kill-session -t claude; tmux new-session -d -s claude 'cd ~/app && claude --channels plugin:telegram@claude-plugins-official --dangerously-skip-permissions --model opus[1m] --effort max'"`
- **Logs:** `su - forwarder -c "tmux capture-pane -t claude -p -S -50"`
- **Bot token:** `~/.claude/channels/telegram/.env` (forwarder home)
- **Access config:** `~/.claude/channels/telegram/access.json`
- **Hooks/settings:** `/home/forwarder/.claude/settings.json` and `/home/forwarder/.claude/hooks/`
- **Plugin:** `telegram@claude-plugins-official` v0.0.6, requires Bun (`/usr/local/bin/bun`)
- **Context reset:** ⚠️ Sending `/clear` in Telegram does **not** reset context — the plugin only handles `start`/`help`/`status`, so `/clear` is forwarded as a plain message and does nothing. The context is one continuous session until the `claude` process is restarted (see the restart command above). A real Telegram-triggered reset would need a supervised session.

**Triggering the investigate skill (shorthands for `/investigate`):** A message that starts with `inv ` OR that reports a pick/grading problem or asks why something did/didn't happen (especially with a `t.me/...` link) is an investigation request — invoke the **investigate skill** (a real Skill tool call, so the once-per-investigation lessons hook counts it). Don't answer these ad-hoc.

**When running on VPS via channels**, this Claude instance can run commands directly (no SSH needed). Check `uname -s` or hostname to detect environment (VPS hostname is `pickbot`). As the `forwarder` user, `systemctl` needs `sudo -n` (passwordless sudo works, e.g. `sudo -n systemctl restart grade-daemon.service`) — the bare `stop`/`start`/`restart` aliases are interactive-SSH-only. `git` commit/push work directly from `~/app`.

**Delivery receipts (👀) are automatic via a hook.** A `UserPromptSubmit` hook (`telegram_seen_react.py`) reacts 👀 to every inbound Telegram message the instant the harness receives it — a hard delivery receipt at the harness level (not a model tool call, so it can't be forgotten or lost to a mid-turn crash). **Reaction present = the session received the message; reaction absent after a few seconds = it was dropped, resend it.** Drops happen because the Bot API has **no history/backfill**, so a message sent during a restart window (before the new process's poll loop is connected) is silently lost — and no hook fires for a message the process never received, which is exactly why the *absence* of the 👀 is the tell. Note the resume-notify hook's "▶️ Restarted… copy this back to resume" message is posted by the SessionStart hook and does **not** prove the receive loop is ready; wait for the 👀 on a fresh message before firing the real task.

The tracker and grade daemon share `parse_cache.json` (atomic writes via `os.replace`). The daemon grades picks fast; the tracker handles Telegram reads, parsing, and odds. When the daemon grades a pick, it sets `broadcasted=True` in the cache so the tracker skips it.

**Broadcasting is daemon-only.** The grade daemon is the sole broadcaster (calls `audit.broadcast_results`). The tracker no longer broadcasts — it grades and edits emojis, but the daemon handles result broadcasting and Google Sheets logging. The listener's `_trigger_tracker_soon()` is debounced (one concurrent run max) to avoid race conditions with the daemon.

### Environment files

Two-file split to protect server-only secrets from `syncenv`:

| File | Where | Synced | Contains |
|---|---|---|---|
| `.env` | local + server | ✅ `syncenv` copies this | config that exists on **both** machines |
| `.env.local` | local + server (separately) | ❌ never touched | `TELEGRAM_SESSION`, `BOT_SESSION`, `X_AUTH_TOKEN`, `X_CT0`, `PIKKIT_TOKEN` |

> **Rule: any value that exists only on the server belongs in `.env.local`.**
> `syncenv` *overwrites* the server's `.env` with the local copy, so a key present in the server's `.env` but absent from the local one is **silently deleted** on the next sync. This is not hypothetical: it wiped `X_AUTH_TOKEN`/`X_CT0` on 2026-07-19 and took the Trent watcher down for 2 days without a single alert. `syncenv` is safe to run freely *only as long as this rule holds*.

`.env.local` is loaded after `.env` in both Python code and systemd, so it always wins.

**Regex escaping in `MAPPINGS_CONFIG`:** The JSON value is inside single quotes in the `.env` file, so regex backslashes need **four** backslashes (`\\\\`) to survive: shell quotes → JSON string → regex. For example, `\d+` becomes `\\\\d+` in `.env`.

**Setting up `.env.local` (first time, on each machine):**

```bash
python scripts/get_session.py      # generates TELEGRAM_SESSION
python scripts/get_bot_session.py  # generates BOT_SESSION
```
Run these **on the VPS** to tie the VPS sessions to `209.38.51.86`. Run **locally** for local dev sessions.

**After writing `.env.local` on the VPS, fix permissions:**
```bash
chmod 600 /home/forwarder/app/.env.local && chown forwarder:forwarder /home/forwarder/app/.env.local
```

---

## Log colors

Colors and formatting are applied by `_fmtlog` in `/root/.server_aliases.sh` — **do not add ANSI codes to Python print statements**. Edit that file directly on the server (no restart needed).

---

## Pick tracker

**`grade` alias uses `--days 1`** — for older picks, run manually:
```bash
su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --days 2 2>&1"
```

**Targeted runs:** `--target=CH:ID` (repeatable) processes exactly those messages and skips the
date scan. Use `=`, not a space — the channel id starts with `-` and argparse would read it as
an option.
```bash
su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --target=-1002486251914:3409 2>&1"
```

**Fast path after a forward:** the listener enqueues the exact `(dest_channel, msg_id)` pairs it
just created and runs a targeted tracker pass ~3s later, so odds land in seconds instead of
waiting for the 5-min timer. A pick fanned out to N dest channels enqueues N targets and batches
into one pass; a forward landing mid-run re-arms the queue and gets the next pass. **Every run
logs** — one summary line per run to `journalctl -u telegram-forwarder` (grep the message id to
answer "did the fast path fire?") and full tracker output to `logs/tracker_quick.log`
(size-rotated, `.1` backup).

**Ungradeable legs are capped, not retried forever.** `claude_grade` returning UNKNOWN is *not* a persisted verdict — only WIN/LOSS/PUSH are — so a leg whose ESPN context builds but which Claude can't resolve stays "unresolved" and would be re-graded by the daemon (~every 60s) and the tracker (every 5min) indefinitely. `should_skip_unknown()` / `record_unknown_attempt()` (`common.py`, shared by both loops so they can't diverge) count attempts on the leg and space them out: up to `GRADE_UNKNOWN_MAX_ATTEMPTS` (default 6) tries at `GRADE_UNKNOWN_BACKOFF_MIN` (default 30) minute intervals, so a late-posting box score still gets ~3h of retries while an unresolvable pick costs ~$0.03 total instead of $9.40/day. Attempt state is a **verdict-less dict** inside `leg_verdicts` — every resolved-check reads it as unresolved, so no broadcast/emoji/eviction behaviour changes — and deliberately carries no `game_date`, because the tracker invalidates a cached leg whose `game_date` disagrees with a day hint, which would reset the counter and restore the loop. A leg going terminal logs `⏹ … ungradeable after N attempts`.

**An entry with one never-resolving leg is retired, and audit never repeats itself.** The attempt cap above only counts tries that reach `claude_grade`; a leg whose game can't be *found* at all returns `CONTEXT_SKIP` → UNKNOWN for free, so it never trips the cap. That is a name failure, not a sport failure — sport and date were right and the scoreboard loaded; the parsed team/fighter ("Ihor Medic" for a card whose main event was Uroš Medić) simply matches no competitor on it, and it will still match none tomorrow. Note a pick-level `sport: null` is normal and harmless — it falls back to the parse's top-level sport. An entry only settles once **every** leg has a verdict, so one such leg pins the whole entry open: its resolved siblings stay `broadcasted=False`, and the tracker's edit path re-records them to the audit channel every run — 576 posts/day at zero API cost, which is why no spend alarm catches it. **Nothing bounds this on its own:** `_EVICT_AFTER_DAYS` looks like a 14-day backstop but `_evict_stale` skips any entry that is not fully resolved, so a stuck entry is exactly the kind it never evicts (there were entries still churning 47 days on). Two independent bounds, both added because there were none:
- `audit.record` fingerprints what the post would *render* (verdict, pick list, calc, sport, date, dry/edit-failed flags) into the `grades` row and **skips the Telegram post when it matches the previous one** for that message. A changed verdict or a new edit failure still posts; a re-grade that says the same thing doesn't. This bounds the whole class regardless of cause. First run after deploy posts once per affected message (stored fingerprint is NULL), then settles.
- The daemon retires an entry whose legs are still unresolved `GRADE_STALE_UNRESOLVED_DAYS` (default 3) past the latest date it could be waiting on. That horizon is the **game** date (from a graded sibling or the odds match), not the post date — aging off the post would kill a Monday pick on Saturday's game. Retirement reuses `_failed`/`_failed_reason` and **never broadcasts**: legs we couldn't grade mean a result we can't assert. It notifies audit once. The tracker skips any `_failed` entry *carrying a reason* before its parse-failure branch, which retries on text change and would otherwise resurrect it.

**Every pick is checked against the slate, and "couldn't verify" is no longer silent.** Those two bounds above only stop a bad parse from *spamming*; `verify_picks_on_schedule()` (`scores.py`, called from `tracker.py` on fresh parses only) stops it from happening. It exists because `validate_sport` answers "does this team have a game?" by returning its arguments unchanged — which is also what it returns when it gives up, so **confirmed and hallucinated were the same value to the caller**, and it only ever ran on `picks[0]` (plus legs carrying one of six sports; UFC wasn't among them). Per pick it reports:
- `confirmed` — really competing. `corrected` — wasn't, but exactly one token from the **raw message** resolves, so use that. This is the actual repair: the capper wrote "Medic", which matches Uroš Medić exactly, and the parse threw it away by expanding a surname into a full name it guessed at ("Ihor Medic", on no card anywhere). Legs confirmed in pass 1 **claim** their competitor first, or a parlay would offer every leg's token to the broken leg and it would give up as ambiguous. A correction rewrites `description` too — that field is load-bearing, and `_swap_team_in_desc` now falls back to replacing everything ahead of the bet-type keyword, because its three patterns all miss precisely when the parse garbled the name worst ("Meditbe Moneyline").
- `suspect` — the quiet, dangerous one: the parsed name IS competing, just in a different contest ("Medic/Rakic parlay" parsed as Mateusz Rębecki, who really did fight that night). It finds a real market, prices it, and grades to a clean verdict, so nothing downstream can tell it from a correct pick. Tell: nobody in the message names this competitor while a word they *did* write points at someone still unclaimed. **Both halves required** — that conjunction is what keeps a nickname the slate doesn't spell out ("Snakes" → D-backs) from tripping it. Reported, never auto-corrected.
- `unverified` — nothing resolves, several do, or the message names a rival promotion (`_RIVAL_PROMOTIONS`: a PFL parlay classified as UFC would otherwise have its "Medic" leg bound to UFC's Uroš Medić — stuck-and-flagged beats confidently-wrong). `n/a` — no schedule for that sport, or the fetch failed: **an ESPN outage must never read as a bad parse.**

Checks `day_hint`, the post date **and the next day** — cappers routinely post the night before, and verifying only the post date returned "no schedule" for a large share of picks. Costs no Claude calls and no extra HTTP (reuses `scoreboard_cache`). Measured over 233 entries / 268 picks: 243 confirmed, 20 n/a, and **4 flags, all four real defects — zero false positives**. When adding a sport, `_RIVAL_PROMOTIONS` is where you note the competitions that share its bucket but not its schedule.

**NHL 3-way / regulation moneyline:** Must win in regulation — OT = LOSS. Detection centralized in `is_regulation_ml()` (`common.py`).

**Cross-league nickname collisions are resolved by the SCHEDULE, not the prompt.** `_AMBIGUOUS_SPORTS` / `validate_sport()` key on fragments of the *parsed* team name, which only works when the shared word survives into the canonical name ("rangers" is in both "Texas Rangers" and "New York Rangers"). Some nicknames leave no shared fragment: "Snakes" is the Arizona Diamondbacks (MLB) **or** the Maryland Whipsnakes (PLL). That ambiguity exists only in the raw message and is erased the moment Claude commits to a canonical name — after which nothing downstream can tell a right resolution from a wrong one, since both find a real game, a real closing line, and grade cleanly to **opposite** verdicts. `resolve_nickname_collision()` (`scores.py`, keyed on the RAW message token via `_NICKNAME_COLLISIONS`) runs in `tracker.py` **before** `validate_sport` on fresh parses only, and picks whichever candidate actually has a game that date. When several do, it drops candidates whose game already started before the post, then applies a deliberately narrow lead-time tiebreak (`_COLLISION_NEAR_HOURS=2` / `_COLLISION_FAR_HOURS=6`); anything less lopsided is **flagged to audit, never guessed**. It rewrites the pick `description` too — that field is load-bearing for grading and tag placement, so leaving the displaced team in it hands the grader a contradiction. Adding a collision = one line in `_NICKNAME_COLLISIONS`.

**KBO (Korean Baseball):** Graded via `koreabaseball.com` ASMX endpoint (`fetch_kbo_context` in `scores.py`). The Odds API has KBO odds but never populates scores, so we scrape the official site instead. Picks are always sent the US evening before the game day, so the code fetches `date+1` to find the correct game. Team ID map (`KBO_TEAM_IDS`) is in `scores.py`. If a pick re-parses as `sport: "Other"` despite the message containing "kbo", the post-parse correction in `claude_parse` (`ai.py`) should catch it.

## Odds integration

To force a re-fetch after manually restoring a cache entry: delete the `odds_by_pick` key from the relevant `parse_cache.json` entry — the next run will re-fetch and re-edit.

> ⚠️ **Only safe before first pitch.** The tracker fetches via `fetch_odds_current`, so a re-fetch after the game starts returns **live** prices instead of the pregame lines. To repair odds on a game already underway, write the values in directly: pull the closing lines with `odds._try_pregame(...)` against the correct event id and set `game_date` yourself. Cross-check by confirming a leg whose cached odds were already correct reproduces exactly.
>
> It used to be worse — the re-fetch could match the *next game in the series*, one day away, slipping under the `>2 days` wrong-game guard and making the bad `game_date` become `eff_date`, which pointed grading at the wrong game. That is fixed: `_find_event_id` no longer treats "already kicked off" as strictly worse than any future game (`_STARTED_GRACE_H`), so a game in progress beats the same matchup tomorrow. See the event-matching note below.

**A pick binds to an event by team name, and an absent game binds to the wrong one.** `_find_event_id` (`odds.py`) scores every event sharing a team name and returns the best — it never asks whether that event is plausibly the pick's game. So when a league's games are *missing* from the sport key we query, the pick doesn't miss: it silently prices off the same team's nearest listed game. The Odds API files preseason under separate keys, which is how a Panthers/Cardinals **preseason** total got Bears @ Panthers' Week 1 price (+320 against a real ~-125) and a Cardinals preseason ML got +455.

Three things keep that closed, all in `_find_event_id`/`fetch_odds*`:
- **`_EXTRA_SPORT_KEYS`** — extra keys carrying the same league in another phase (`americanfootball_nfl_preseason`, NBA/NHL preseason, MLB spring training). Event lists from every candidate key are merged and scored together and each event's own `sport_key` is used for the odds call. **Adding a league phase = one line here.** Out of season a key returns HTTP 200 + `[]`, and `/events` costs no quota (`x-requests-last: 0`), so carrying an unused key is free.
- **`as_of`** — events are ranked against the date the pick is *about*, not `datetime.now()`. Ranking by date rather than clamping to a window is deliberate: a UFC card is routinely posted five days early, so any fixed window breaks it.
- **Fail-closed on partial team matches** — a pick naming two teams that must be opponents rejects an event holding only one of them, *but only when the other is demonstrably on the schedule elsewhere*; a name variant `_team_matches` can't resolve must still fall through, or this drops odds we get right today.

Don't "fix" a wrong price by widening a guard downstream: the `>2 days` guard in `tracker.py` already fired here and the historical fallback re-ran the same matcher, re-matched the same game, and returned the identical price — with no `game_date` to show for it. `scripts/test_event_match_regression.py` pins all of this offline.

**A line the pick matched but that already states the capper's price IS the pick's line — stop there.** `_insert_odds` (`tracker_format.py`) skips such a line so we don't restate a number the capper already showed, and `_place` returns `None` for it. That `None` means "keep looking" for a line carrying a *different* pick's tag, but applying it to the source-price case walks the loop past the bet and onto whatever later line also matches — the write-up that names the team again, or an angle record holding the bet's number ("9-3 off 2 wins" for an over 9). The price then renders after the analysis instead of on the bet. Any such line resolves through the `src_declined` rule instead: quiet inside the 3% move threshold, `[-190 now]` on the pick line beyond it. `scripts/test_source_priced_odds.py` pins both halves offline (fall-through *and* later-line match). Note the sibling trap when repairing these live: the tracker rebuilds each edit from the current message text, so a tag on the wrong line must be **stripped**, not just re-placed.

**Backtest / audit:**
```bash
python scripts/audit_odds.py --days-back 7
python scripts/audit_odds.py --dry-run
```

## Pikkit betting splits

`pikkit.py` fetches community betting splits (bet% + handle%) from Pikkit's API for each pick. Tracker calls `get_pick_splits()` after odds fetch and stores results in `pikkit_by_pick` in parse_cache. The dashboard shows a Book Interest column/filter based on this data.

- **API:** `prod-website.pikkit.app` — `/events/all` (event discovery) + `/event/foryou/{id}` (splits by market)
- **Auth:** `PIKKIT_TOKEN` in `.env.local` (opaque hex session_id, 400-day expiry)
- **Key constraint:** completed games return 403 — splits must be fetched before/during the game
- **Token generation:** `scripts/pikkit_auth.py` — must run locally (real Chrome + display), NOT on VPS. Turnstile rejects headless/bundled Chromium. Session is NOT IP-bound (tested 2026-07-24).

**Two-step manual auth flow (run locally):**
```bash
python scripts/pikkit_auth.py --send-sms --phone +19545361686
# Read SMS code, then:
python scripts/pikkit_auth.py --submit-code <CODE> --auth-id <from step 1> --phone +19545361686
```
Token is saved to `.env.local`. SCP to VPS or update `.env.local` there manually. `PIKKIT_TOKEN` must be in `.env.local`, NOT `.env` — `syncenv` would wipe it.

**Token health check (from VPS or locally):**
```bash
python scripts/pikkit_auth.py --validate
```

## Broadcast results

**`_format_pick` (`audit.py`) is the one renderer — every branch must interpolate `period_tag`.** It builds the label for the broadcast, the merged multi-capper broadcast, *and* the Google Sheets log (`sheets.py` imports it), so an omission there is wrong in three places at once. The tag is computed once at the top and then each `bet_type` branch formats its own string, which is exactly how a branch forgets it: totals and player props dropped it, so `.5u Wings 1H u79.5` posted as `Dallas Wings U79.5` — a different bet at a different line, and one nothing downstream can flag because a missing qualifier is indistinguishable from a game-period pick. Spread/ML are the format to match (`Toronto Argonauts 1H ML`); the tag goes after the team/player, before the bet. `scripts/test_period_tag.py` pins every bet type offline — **adding a `bet_type` branch means adding its case there**, including the MLB/KBO `1h`→`F5` rename.

**Testing workflow** (reset emojis and re-run locally):
```bash
python scripts/clear_emojis.py --channel -100xxxxxxxxxx  # strip emojis (today)
python scripts/clear_emojis.py --days 2                  # last 2 days
python tracker.py --live --channel -100xxxxxxxxxx        # re-grade + broadcast
```

## Sauce daily (Kyle Kirms)

`scripts/sauce_daily.py` scrapes the SAUCE tab, grades picks, renders an image (Pillow), and sends it to channel `-1003977774560`. Runs daily at **6 AM ET** via cron on the VPS (`run_sauce_daily.sh`).

- **Google Sheet:** `1yozWEoQ5m6rqNC8-E5UGwg0ySjYbAybNHwPmtNTYIzM` (shared with service account)
- **Source data:** Published Google Sheet embedded at kylekirms.com/open-bets (sheet ID `1yjaN85i-WRhRrBcozOG70vTX6cTNpJzFmuNJ8KgL-14`)
- **DB table:** `sauce_picks` in `picks.db`
- **Cron log:** `/tmp/sauce_daily_cron.log`
- **Image rendering:** Uses **Pillow** (`render_image_pil` in `sauce_daily.py`), rendered in-process — no Chromium. Switched off Playwright (commit e252302) because the headless-Chromium render tree OOM'd on the ~1GB/no-swap VPS. Requires `fonts-liberation` on the VPS (`/usr/share/fonts/truetype/liberation/`); result marks are vector-drawn (check/cross/circle/?), not emoji.

**Manual run on VPS:**
```bash
su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/sauce_daily.py --channel -1003977774560 2>&1"
```

**ESPN sport validation:** `validate_sport()` in `scores.py` verifies Claude's sport classification against ESPN game schedules. Catches ambiguous teams (Rangers, Cardinals, Giants, etc.). Also wired into the core tracker flow in `tracker.py`.

## Capper backfill (Twitter → Google Sheet)

**One command does the whole thing.** `scripts/backfill_capper.py` runs all five stages, then prints the exclusion table and the record:

```bash
python scripts/backfill_capper.py --account boyerBets_ --since 2026-01-01
```

Stages, each persisting to `scripts/output/<account>_*.csv`:
`fetch_x_posts` → `parse_posts_csv` → `grade_csv` → `format_graded_csv` → `sheets_export`

Because every stage persists, re-run only what you need instead of paying for the parse again (full parse of ~600 tweets ≈ $6; grading ~100 picks ≈ $0.50):

```bash
python scripts/backfill_capper.py --account X --from grade          # reuse the parse
python scripts/backfill_capper.py --account X --only export         # just push to Sheets
python scripts/backfill_capper.py --account X --since D --limit 20  # cheap trial
```

- **The fetch runs on the VPS by default** (X cookies live in its `.env.local`) and **auto-pauses `trent-monitor.timer`**, restoring it even if the fetch crashes — both share one X session and UserTweets has a ~15 min cooldown, so a concurrent run makes one of them fail. `--local-fetch` if cookies are set locally; running *on* the VPS is detected automatically.
- **Subprocesses get `PYTHONIOENCODING=utf-8`**, so the Windows `cp1252` crash on emoji output can't happen.
- **The excluded-picks table prints every run** and is written to `<account>_excluded.md` with tweet links. Not optional detail: only `pod` is graded by default, and the `result` category is picks revealed *after* they won — grading those manufactures a fake record. URLs come straight from the CSV; never reconstruct one from a tweet id.

### Google Sheets export

`scripts/sheets_export.py` writes `<account>_sheet.csv` into **one tab per account** in a shared workbook (`BACKFILL_SHEETS_ID` in `.env`). Numbers are sent as numbers so the Return column sums without cleanup; header is frozen + filtered, W/L cells colour-coded.

One-time setup: share the workbook as **Editor** with `forwarder@api-project-349700129720.iam.gserviceaccount.com`. The script cannot create the workbook — the project's **Drive API is disabled**, so `gspread.create()` 403s; only the Sheets API is on, which suffices for writing into an already-shared workbook. (Enabling the Drive API would allow create+share, but that's console work this avoids.) A missing share prints a one-line fix and the pipeline still leaves the CSV on disk.

### Odds sanity check

`format_graded_csv.py` flags SPREAD/TOTAL priced outside 1.65–2.35 or MONEYLINE outside 1.15–4.00, and reports how many are on **wins** — only those move the P&L. Added because a wrong game date silently produced a Blazers +11.5 at **1.22 (−455)**, not a real spread price; a wrong date returning a *plausible* price would leave no trace at all. When it fires, check the game date first.

## Twitter/X pick parsing

`scripts/parse_posts_csv.py` parses a capper's tweets CSV (from `fetch_x_posts.py`) to extract official pick placements. Three-phase pipeline:

1. **Text parse** — sends each tweet to Claude to determine if it's an official pick announcement (not commentary, celebration, or reaction)
2. **Image parse** — for posts with pick signals in text but no extractable pick (bet slip in attached image), downloads the image and sends it to Claude
3. **Dedup** — removes duplicate tweet IDs and duplicate picks (same day + normalized teams + same bet_type)

```bash
python scripts/parse_posts_csv.py              # full run
python scripts/parse_posts_csv.py --limit 10   # test on first 10 rows
python scripts/parse_posts_csv.py --skip-images # text-only (cheaper)
```

**Input:** `scripts/output/<Account>_posts.csv` (from `fetch_x_posts.py`)
**Output:** `scripts/output/<Account>_parsed.csv` with structured columns: sport, description, bet_type, teams, player, prop_stat, line, direction, period.

Key design decisions:
- RT filter (`_is_retweet`) skips retweets before hitting the API
- Team name normalization (`_normalize_team`) handles variant spellings for dedup (e.g. "Bosnia" vs "Bosnia and Herzegovina")
- No hardcoded exclude lists — all filtering is via prompt rules and algorithmic dedup so the script works for any capper's account

## CSV pick grading

`scripts/grade_csv.py` batch-grades a parsed CSV (from `parse_posts_csv.py`) using the live grading pipeline (ESPN scores + Claude). Filters by sport and adds `grade`/`calc` columns.

```bash
python scripts/grade_csv.py --account boyerBets_                 # every sport
python scripts/grade_csv.py --account boyerBets_ --sport NBA     # one sport
python scripts/grade_csv.py --account boyerBets_ --limit 5       # first 5 matching
python scripts/grade_csv.py --account boyerBets_ --categories all  # ignore the POD filter
```

Only rows whose parse `category` is in `GRADEABLE_CATEGORIES` (just `pod`) are graded by default — see the backfill section above for why that matters.

**Soccer moneyline grading:** Soccer moneyline is 3-way — a draw is a LOSS, not a push. Only DNB (draw no bet) pushes on draws. "To advance" / "to qualify" picks use the final result (including extra time / penalties). This rule is in `_GRADE_PROMPT` in `ai.py`.

`scripts/format_graded_csv.py` converts graded CSV → spreadsheet format (Sharp Syndicate layout). Odds sourced from: description text first, then Odds API historical closing lines (exact matches only), then -110 default for any gaps.

## Trent watcher (@BookitWithTrent)

`scripts/trent_watcher.py` polls @BookitWithTrent on X/Twitter every 15 minutes via systemd timer, detects official pick announcements using Claude (yes/no classification), and forwards the original tweet content (text + images) to channel `-1004394797084`.

- **Systemd:** `trent-monitor.timer` (15 min) → `trent-monitor.service`
- **DB table:** `trent_seen` in `picks.db` (tracks processed tweet IDs, pruned after 7 days)
- **X credentials:** `X_AUTH_TOKEN` and `X_CT0` in **`.env.local`** (browser cookies from x.com, may expire). They must live in `.env.local`, NOT `.env` — `syncenv` overwrites `.env` from the local machine, and since the local `.env` has no X keys, putting them there silently wipes them on the next sync (this took the watcher down for 2 days on 2026-07-19). If the cookies are missing or rejected, the watcher now exits non-zero and DMs the operator via the watchdog bot (rate-limited to once per 6h; state in `~/.trent_watcher_state.json`).
- **twscrape wrapper:** always build the API via `scripts/x_client.py` (`build_api()`), never `API()` + `add_account_cookies()` directly. It carries two workarounds for library bugs that both present as "bad cookies": twscrape's XClIdGen scrapes a page X has since migrated (fix: point it at `https://x.com`), and `add_account_cookies()` silently ignores rotated cookies when the account is already cached in `accounts.db`. See the module docstring.
- **Lookback:** 2 hours per run (covers missed runs / gaps)
- **Channel grading:** Channel is in `GRADE_CHANNELS` — tracker handles odds + result emojis
- **Singles only.** This channel forwards SINGLE-GAME bets; multi-leg parlays are not sent. Three checks enforce it, and the order matters: `is_pick_text` decides *whether* it's a pick, `is_pick_image` is a fallback that can only rescue a tweet the text rejected, and `is_parlay_image` is a **veto** that runs on every candidate with a photo — *including* ones the text already approved. That last one exists because the tweet text often names just one leg of the slip ("Swapped Braves ML for Red Sox ML" attached to a 5-leg FUGAZI FIVE), so the image is the only ground truth about what the bet actually is. Keep the veto's prompt narrow and default-to-pass: a false positive silently stops real picks, and nothing downstream would flag it.

**Manual run on VPS:**
```bash
su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/trent_watcher.py --dry-run 2>&1"
su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/trent_watcher.py --lookback 24 2>&1"
```

**Message format:** `◼️ Trent\n\n{original tweet text}\n\n{tweet URL}` with images attached. t.co media links stripped from text.

**Rate limits:** Twitter's UserTweets endpoint has a ~15 min cooldown. Script wraps fetch in a 90s timeout — exits cleanly if rate-limited, retries next run.

## Angle Analyzer

`angles/extract_angles.py` scrapes channel `-1002486251914` for picks with blockquoted angle records, parses them into structured data (type, sport, bet type, side, day, unit, time window, off-count, undefeated/winless), enriches with grades from `picks.db`, and outputs `angles/data/angles.json`. Picks without angles get a `no_angle` type for baseline comparison.

`angles/index.html` is a single-file dashboard (Tailwind + Chart.js) that loads the JSON. Features: multi-filter bar (pick-level and angle-level), KPIs, cumulative profit chart, Quick Breakdown pivot table (group by any dimension), searchable/sortable picks log with parsed angle display, CSV export.

**Hosted at:** `https://fightclubpicks.cc` — served by `angles/server.py` (Python stdlib, ~10MB RAM) behind Cloudflare (HTTPS, DDoS protection). Domain: Cloudflare Registrar, A record → `209.38.51.86` proxied.

**Authentication:** Access is gated behind Telegram membership in the Fight Club channel (`-1002486251914`). Users send `/access` to `@forwarder_fc_bot` (or click the deep link on the login page at `/login`). The bot checks membership via Bot API `getChatMember` and replies with a magic link (`/auth?token=...`) valid for 5 minutes. Clicking the link sets an HMAC-signed session cookie (`aa_session`) lasting 30 days. Auth module: `angles/auth.py` (stdlib only, stateless HMAC-SHA256). The `/access` handler lives in `listener.py` on the bot client.

**One-click refresh:** The dashboard has a "Refresh Data" button that streams real-time progress via SSE. Uses the session cookie for auth (no separate API key).

**Systemd:** `angles-dashboard.service` — persistent, port 80, runs as forwarder user.

**Activity dashboard:** Admin-only route at `/activity` tracks page views, unique visitors, and who visited (with Telegram display names). Logging is purely server-side (zero client-side network calls). Data stored in `angles/data/activity.db` (separate from picks.db). Username resolution via Bot API, cached 7 days.

**Env vars:**
- `ANGLES_AUTH_SECRET` — HMAC key for signing auth tokens/cookies (required)
- `ANGLES_PORT` — listen port (default 80)
- `ANGLES_ADMIN_IDS` — comma-separated Telegram user IDs that can access `/activity` (empty = all authenticated users)
- `BOT_TOKEN` — used to resolve Telegram user IDs to display names on the activity dashboard (optional, falls back to numeric IDs)

**Manual data pull (on VPS):**
```bash
su - forwarder -c "cd ~/app && ~/venv/bin/python angles/extract_angles.py"
```

Angle types: `run`, `off_losses`, `off_wins`, `sport_record`, `bet_type_record`, `side_record`, `day_record`, `time_scoped`, `unit_record`, `no_angle`. Prose lines with records buried in sentences are auto-skipped. Context headers (e.g. "L30 days:", "This month:") propagate scope to subsequent bare-record lines. Parenthetical sub-records inherit sport/bet_type/side from parent context.

## Infra sync

`deploy/` is the source of truth for systemd units and Claude Code hooks. Edit files there, commit, then push to live:

- **Systemd units:** `sudo cp deploy/systemd/<unit> /etc/systemd/system/<unit> && sudo systemctl daemon-reload && sudo systemctl restart <unit>`
- **Hooks:** `cp deploy/hooks/<hook> ~/.claude/hooks/<hook> && chmod +x ~/.claude/hooks/<hook>`

**Detect drift:** `bash scripts/check_deploy_sync.sh` — diffs every file under `deploy/` vs its live VPS copy, prints OK/DRIFT per file, exits non-zero on drift.

### Writing a `run_*.sh` runner

- **`set -o pipefail` is mandatory.** These scripts pipe Python through `tee` to a logfile, and a pipeline's exit status is *`tee`'s* — always 0. Without it a crashed job reports success, skips its own retry, and pings healthchecks.io as OK. All three runners silently did this until 2026-07-21.
- **`TimeoutStartSec` must exceed the runner's own worst case** (retries × per-attempt timeout + backoff), or systemd kills the retry mid-flight.
- **Set the `*_HEALTHCHECK_URL`** for the job in `.env`. `ping_hc()` is a silent no-op when unset, so the script *looks* monitored while having no dead-man's-switch at all. Coverage today: listener ✅, tracker ✅, **trent ❌ (`TRENT_HEALTHCHECK_URL` unset)**, **sauce ❌ (`run_sauce_daily.sh` pings nothing and has no retry)**. Sauce is the proof of what that costs: it crashed at the same step three mornings running (2026-07-29→31) and nobody found out until the missing posts were noticed by eye.

## Deploy workflow

`syncenv` runs **locally** to push `.env` to the VPS, then deploy on the VPS:

```bash
# Local
syncenv
git push

# On VPS (via SSH)
ssh root@209.38.51.86 'cd /home/forwarder/app && git pull && systemctl restart telegram-forwarder'
```
