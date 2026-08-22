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
- `claude-spend-watchdog.timer` — Claude API spend monitor (`deploy/claude_spend_watchdog.py`), hourly at :07. Silent unless it DMs the operator via the watchdog bot: 💸 when trailing-24h spend exceeds `CLAUDE_SPEND_DAY_ALERT_USD` (default $3), ⚡ when trailing-1h exceeds `CLAUDE_SPEND_HOUR_ALERT_USD` (default $0.60 — catches a fast leak before the daily figure moves). Reuses `WATCHDOG_BOT_TOKEN`/`WATCHDOG_USER_ID`; state in `~/.claude_spend_watchdog_state.json`; debounce 12h daily / 6h hourly. Check the numbers by hand any time with `--report` (prints, sends nothing; it also names which source it read). **Added because spend had no signal at all:** one mis-parsed pick re-graded in a loop cost ~$9.40/day for five days before anyone thought to grep for it.

  **Spend is counted at the choke point, not scraped from journald.** `ai.py`'s `_claude_create_with_retry` is the one function every Claude call passes through, and it appends `{ts, usd, source}` to `logs/claude_spend.jsonl` (gitignored, pruned past `CLAUDE_SPEND_LEDGER_DAYS`, default 30). It replaced a journald scan of four systemd units, which **could not see any caller that wasn't one of those units** — `sauce_daily` runs from cron and the listener's fast-path tracker is a subprocess, so both logged to plain files (`/tmp/sauce_daily_cron.log`, `logs/tracker_quick.log`) and neither timestamps its cost lines, meaning they can't even be windowed into trailing-24h/1h after the fact. Together ~$0.14/day against the ~$0.15/day the scan could see: the figure ran **~40% low**, so the $3/day threshold really tripped nearer $5. Parsing those two logs would have fixed the instance and still missed the *next* entry point; an append at the choke point is complete by construction — cron, manual backfill scripts and any future service included. `source` defaults to `argv[0]`, overridable via `CLAUDE_SPEND_SOURCE` (the listener tags its quick run `tracker-fastpath` to separate it from the timer's `tracker`). journald remains the **fallback** for a window the ledger doesn't reach back through yet, and the two are **never summed** — for those four units they describe the same calls. Adding a new Claude entry point needs nothing; it is counted the moment it calls `ai.py`.
- `odds-quota-watchdog.timer` — Odds API quota monitor (`deploy/odds_quota_watchdog.py`), hourly at :23. Silent unless it DMs the operator via the watchdog bot: 🛑 when the monthly quota hits zero, 📉 when remaining falls under `ODDS_QUOTA_LOW_REMAINING` (default 2000) with a burn rate and projected exhaustion date. Reuses `WATCHDOG_BOT_TOKEN`/`WATCHDOG_USER_ID`; state in `~/.odds_quota_watchdog_state.json` (last notified condition, plus usage samples for the rate). **It alerts on a state CHANGE, not on a timer**, and sends ✅ when the quota comes back — the event you're actually waiting for. It used to re-send the same 🛑 every 6h, which over one outage-to-monthly-reset is ~72 identical messages about a condition the operator already knew and could do nothing about; that is how a real alert becomes something you swipe away. **Repeats are off by default** (`ODDS_QUOTA_REMIND_SECS=0`): an unchanged condition is not news, and the ✅ already answers "is it back yet?" without anyone having to ask. Set it to a positive number of seconds to re-nag while a condition persists. Same principle as `audit.record` fingerprinting: never re-post a message that says what the last one said. The probe is free — `/v4/sports/` returns the usage headers at `x-requests-last: 0`. `--report` prints and sends nothing. **Added because running out has no signal whatsoever:** `fetch_odds*` catches the 401 and returns `match_type="no_game"` — the *same* value a genuinely missing event produces — so picks just quietly stop showing a price. And it doesn't self-heal: the tracker fetches odds once per pick and reuses `odds_by_pick` forever after, so every pick posted during an outage stays priceless even after the quota resets. Repairing those needs historical closing lines (`odds._try_pregame`), not a re-fetch — by then the games have started and a re-fetch returns live prices (see the odds-repair warning above). Hit 20,000/20,000 on 2026-08-08.

  **Quota accounting reads `x-requests-last`, never a difference of `x-requests-remaining`.** The vendor states each request's exact cost in that header. The old code instead subtracted successive `remaining` values — which has no predecessor on the **first request of a process**, so every run's first (and usually only) paid call went uncounted. That is why a month that really spent 20,000 credits logged ~300, and why the ceiling arrived with no warning from a monitor that looked healthy.

  **Requests are narrowed to the pick's own market at its own period (`_narrow_markets_for_pick`), in one region.** Two independent multipliers, both cut on 2026-08-09 against measured data rather than guesswork:
  - **Markets** — the `MARKETS_BY_TYPE` lists ask for every period variant (8–34 markets) and we are billed for each that exists. A pick reads 1–2. Narrowing keeps the `alternate_*` sibling, because **22 of 208 priced picks (10.6%) match through it** (`exact_alt`) — five times the cost of dropping a region. The alternate conditions are **not uniform** (spreads: game + MLB innings only; totals: also hockey periods; team totals: always; h2h: never) and are mirrored from the `_lookup_*` functions in `_alt_market_for`; `scripts/test_market_narrowing.py` pins the two in sync, in both directions — a market the lookups read but we stop requesting silently loses the price, and one we request but never read is billed forever.
  - **Regions** — `REGIONS` (env `ODDS_API_REGIONS`, default `us`). Dropping `us2` (ballybet, betanysports, betparx, espnbet, fliff, hardrockbet*, rebet) was measured over 3,076 cached outcomes: it is the sole source for **2.1%** and beats every `us` book on **3.0%** by a median 0.82 points of implied probability. Set `ODDS_API_REGIONS=us,us2` to restore. Responses are cached under a region-namespaced key (`_cache_markets`) so a narrower fetch is never served to a caller expecting full coverage.

  Replaying the whole cached history: **3,236 → 925 credits (71% less)**; the plan now buys ~1,818 priced picks/month instead of ~487. `set_economy(True)` drops the alternate too (80% less) — backfills only, where a miss falls back to −110.
- `claude-auth-watchdog.timer` — Claude credential monitor (`deploy/claude_auth_watchdog.py`), every 6h at :23. Silent unless it DMs the operator via the watchdog bot: 🔑 when the VPS Claude session's credentials are rejected, plus a one-line all-clear when they work again. Debounce 12h; state in `~/.claude_auth_watchdog_state.json`. Check by hand with `--report`, or `/auth` in the bot. **Probes `api.anthropic.com` directly — it must never shell out to `claude`:** a second CLI process on this box churns the channels plugin's bun poller, which trips `run_claude_channels.sh`'s "bun/telegram plugin gone" check and restarts `claude-channels.service`, so a CLI-based probe would periodically reboot the session it monitors and wipe its context. One 1-token haiku call, ~$0.00002. **Only 401/403 counts as dead** — a 429, a 5xx or a network error stays silent, for the same reason an ESPN outage must never read as a bad parse.
- `claude-watchdog-bot.service` — interactive watchdog bot (`deploy/claude_watchdog_bot.py`). Uses `WATCHDOG_BOT_TOKEN`. Menu commands: `/mem` (RAM/swap usage), `/status` (service status), `/restart`, `/kill` (force-kill+restart), `/logs` (last 20 journal lines), `/tmux` (Claude's current pane), `/auth` (credential check), `/reauth` + `/authcode` (re-auth from a phone, see below). Commands set via `setMyCommands` Bot API.

### Claude Code via Telegram (Channels)

Claude Code runs on VPS in a tmux session with the official Telegram channels plugin. The user DMs `@ForwarderClaudeBot` on Telegram to interact with Claude Code — full CLI features (skills, hooks, memory, dangerous mode) work.

- **tmux session:** `tmux attach -t claude` (as forwarder user)
- **Restart:** `su - forwarder -c "tmux kill-session -t claude; tmux new-session -d -s claude 'cd ~/app && claude --channels plugin:telegram@claude-plugins-official --dangerously-skip-permissions --model claude-fable-5 --effort max'"`
- **Logs:** `su - forwarder -c "tmux capture-pane -t claude -p -S -50"`
- **Bot token:** `~/.claude/channels/telegram/.env` (forwarder home)
- **Access config:** `~/.claude/channels/telegram/access.json`
- **Hooks/settings:** `/home/forwarder/.claude/settings.json` and `/home/forwarder/.claude/hooks/`
- **Plugin:** `telegram@claude-plugins-official` v0.0.6, requires Bun (`/usr/local/bin/bun`)
- **Context reset:** ⚠️ Sending `/clear` in Telegram does **not** reset context — the plugin only handles `start`/`help`/`status`, so `/clear` is forwarded as a plain message and does nothing. The context is one continuous session until the `claude` process is restarted (see the restart command above). A real Telegram-triggered reset would need a supervised session.

**Auth is a 1-year token in `~/.claude/auth.env`, not the credentials file.** `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) is read by `claude-channels.service` via `EnvironmentFile=-/home/forwarder/.claude/auth.env` (0600, never in git; the `-` means a missing file doesn't block startup). It replaced `~/.claude/.credentials.json`, whose **refresh token carries a hard expiry**: when the one on the VPS lapsed at its exact `refreshTokenExpiresAt` (2026-08-11 01:26 UTC), the CLI rewrote the blob with empty `accessToken`/`refreshToken` and every Telegram message got "Login expired · Please run /login". Nothing detected it — systemd still reported the unit `active`, because the *process* was healthy and only its credentials weren't — and it stayed down ~10 days until the operator reached a desktop. The old file is parked at `.credentials.json.disabled`; the token was verified to authenticate on its own with it absent. The banner reads "Claude API" rather than "Claude Max" under an env token, but usage is still on the subscription — the API returns `anthropic-ratelimit-unified-5h/7d` quota headers with `overage-status: rejected` (`org_level_disabled`), i.e. refused at the limit, not billed.

**Re-auth from a phone: `/reauth` in the watchdog bot**, which DMs a login URL; open it, then send back `/authcode <code>`. It verifies the new token against the API *before* installing it, then restarts `claude-channels`. This lives in the watchdog bot for the obvious reason — it's needed exactly when Claude itself can't answer — and works because that bot is a **separate service with its own token**, so it survives both the dead credential and the restart that fixing one requires. `claude setup-token` is interactive (prints a URL, then blocks on stdin), so it runs in a detached tmux session whose two properties are load-bearing, both learned by breaking them:
- **A dedicated socket (`tmux -L authtok`).** On the default socket the tmux server is a child of `claude-channels.service`, so `KillMode=control-group` destroys it on any restart — including the restart re-auth performs at the end. A `setup-token` session started on the default socket was killed mid-flow this way, taking its one-time code with it.
- **A wide pane (`-x 400`).** The CLI hard-wraps output to the pane width, so at 80 cols a 108-char token is captured as 79 chars. That truncated token *looks* like a token and installs cleanly; it only fails later, at the next restart. `finish_reauth` refuses anything under 100 chars and probes the token before writing it.

**Triggering the investigate skill (shorthands for `/investigate`):** A message that starts with `inv ` OR that reports a pick/grading problem or asks why something did/didn't happen (especially with a `t.me/...` link) is an investigation request — invoke the **investigate skill** (a real Skill tool call, so the once-per-investigation lessons hook counts it). Don't answer these ad-hoc.

**When running on VPS via channels**, this Claude instance can run commands directly (no SSH needed). Check `uname -s` or hostname to detect environment (VPS hostname is `pickbot`). As the `forwarder` user, `systemctl` needs `sudo -n` (passwordless sudo works, e.g. `sudo -n systemctl restart grade-daemon.service`) — the bare `stop`/`start`/`restart` aliases are interactive-SSH-only. `git` commit/push work directly from `~/app`.

**Delivery receipts (👀) are automatic via a hook.** A `UserPromptSubmit` hook (`telegram_seen_react.py`) reacts 👀 to every inbound Telegram message the instant the harness receives it — a hard delivery receipt at the harness level (not a model tool call, so it can't be forgotten or lost to a mid-turn crash). **Reaction present = the session received the message; reaction absent after a few seconds = it was dropped, resend it.** Drops happen because the Bot API has **no history/backfill**, so a message sent during a restart window (before the new process's poll loop is connected) is silently lost — and no hook fires for a message the process never received, which is exactly why the *absence* of the 👀 is the tell. Note the resume-notify hook's "▶️ Restarted… copy this back to resume" message is posted by the SessionStart hook and does **not** prove the receive loop is ready; wait for the 👀 on a fresh message before firing the real task.

The tracker and grade daemon share `parse_cache.json` (atomic writes via `os.replace`). The daemon grades picks fast; the tracker handles Telegram reads, parsing, and odds. When the daemon grades a pick, it sets `broadcasted=True` in the cache so the tracker skips it.

**Broadcasting is daemon-only.** The grade daemon is the sole broadcaster (calls `audit.broadcast_results`). The tracker no longer broadcasts — it grades and edits emojis, but the daemon handles result broadcasting and Google Sheets logging. The listener's `_trigger_tracker_soon()` is debounced (one concurrent run max) to avoid race conditions with the daemon.

### Environment files

Environment split to protect server-only secrets from `syncenv` and keep the
NFL Guesser's Odds API quota isolated:

| File | Where | Synced | Contains |
|---|---|---|---|
| `.env` | local + server | ✅ `syncenv` copies this | config that exists on **both** machines |
| `.env.local` | local + server (separately) | ❌ never touched | `TELEGRAM_SESSION`, `BOT_SESSION`, `X_AUTH_TOKEN`, `X_CT0`, `PIKKIT_TOKEN` |
| `.env.guesser` | server only | ❌ never touched | `ODDS_API_KEY` override loaded only by `nfl-lines-fetcher.service` |

> **Rule: any value that exists only on the server belongs in `.env.local`.**
> `syncenv` *overwrites* the server's `.env` with the local copy, so a key present in the server's `.env` but absent from the local one is **silently deleted** on the next sync. This is not hypothetical: it wiped `X_AUTH_TOKEN`/`X_CT0` on 2026-07-19 and took the Trent watcher down for 2 days without a single alert. `syncenv` is safe to run freely *only as long as this rule holds*.

`.env.local` is loaded after `.env` in both Python code and systemd, so it always wins.
For the NFL Guesser only, systemd then loads `.env.guesser` last. Keep the
paid/guesser key there so graders and the quota watchdog continue using the
shared key from `.env`.

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

**Scoreline bets are settled by arithmetic, never by asking Claude to compare numbers.** `try_early_grade_math` (`scores.py`) grades `total`/`team_total`/`spread`/`moneyline` off the linescores. **Totals** settle mid-game once the value has passed the line (a running total is monotone) and **at final outright, including PUSH**. **Spreads and moneylines settle at final only** — a lead is *not* monotone, and calling one early would settle a game that can still be lost.

This exists because grading settled bets was Claude's alone and it got two wrong, both invisible downstream — a wrong verdict prices, grades and broadcasts exactly like a right one:
- `Dallas Wings 1H under 79.5` is `bet_type=total` (the team name identifies the *game* — see the Drake example in `_GRADE_PROMPT`) but was graded as Dallas's own half, 36, instead of the combined 80: a half-point loss booked as a win.
- A `-1` run line won by exactly 1 run is a **PUSH**; it was booked a LOSS. Whole-number spreads and tied side bets refund, and the old `> line` shape could not express that at all.

**Don't try to fix this class with prompt text — the rule was already there and already correct.** `_GRADE_PROMPT` states "PUSH if exactly X" for spreads, and the Drake example for totals; both bugs violated a rule written directly above them. The decisive evidence is the run line: a pick fanned out to two dest channels is graded independently per channel, and the *same* text on the *same* game with the *same* prompt came back **PUSH in one channel and LOSS in the other** (`-1002486251914:3530` vs `-1004339684312:244`). One of those two calls was always going to be wrong, and no wording makes a sampled answer deterministic. Arithmetic on the box score is the only fix that holds.

For the total, two independent defects had to line up: the arithmetic only ran mid-game, *and* `PERIOD_MAP` had no WNBA entry, so `_extract_period_scores` returned None and the path was dead for that sport in every state.

Three rules follow:
- **`PERIOD_MAP` is part of adding a sport**, alongside the parse enum + `ESPN_LEAGUES` + `SPORT_KEYS`. An unmapped `(sport, period)` doesn't error — it silently disables all period math.
- **CFL reaches the math path through `fetch_cfl_scoreboard()`, not `fetch_espn`.** ESPN serves zero CFL events, so the math path was handed an empty scoreboard and declined every CFL pick. It now gets the scraped cfl.ca card shaped like an ESPN scoreboard. It is deliberately *not* wired into `fetch_espn`: `validate_sport` keys off CFL's ESPN scoreboard being **empty** to stop CFL teams fuzzy-matching other sports ("Blue Bombers" → MLB "Blue Jays"), so populating it globally would change sport validation as a side effect. Two traps live in the shaping — `live` and `final` are independent flags, so mapping "not live" to `post` (as the old single-game wrapper did) presents an **unplayed game as a finished 0-0**; and every event needs a **unique id**, since `find_event_ids` returns ids and the caller resolves the first match, so a shared id binds every pick to whichever game came first. The schedule scrape is cached (60s, failures 15s) and shared with the context path, because the daemon would otherwise re-scrape per pick per 10s cycle.
- **`MATH_GRADABLE_SPORTS` is an allowlist**, and unlisted sports fall through to Claude. UFC is why it must be explicit: an event holds many bouts and `_extract_period_scores` reads `competitions[0]`, so a round total would add two unrelated bout scores into a confident wrong verdict. Soccer is out for the extra-time rule.
- **Every guard fails open** (returns `None` → normal context+Claude path). Keep it that way: a guard that swallowed a pick would be worse than the bug it prevents. Anything the scoreline can't express is refused on purpose — regulation/3-way ML (`is_regulation_ml`: the final score includes OT), "to advance/qualify" (decided by the series, not this game), player props, double chance and DNB.

Pinned offline by `scripts/test_total_grade_math.py`, built on the real box scores. Validated by replaying every cached pick against its stored verdict: **197 agree, 0 disagree** on ESPN sports, plus **14 agree / 0 disagree** on CFL against live cfl.ca. That leaves 12 declines, all deliberate — 10 UFC moneylines (a fight card is not a scoreline) and 2 CFL **1Q 3-way** moneylines, refused by `is_regulation_ml` because a tie is a LOSS in a 3-way market and the scoreline cannot express that. Re-run both replays after touching this function; agreement should stay at 100% minus known defects, and a *drop in declines* deserves as much scrutiny as a disagreement — it means a guard stopped firing.

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

**Bovada is the free odds fallback for CFL (`_BOVADA_PATHS` in `odds.py`) — fallback only, the Odds API stays primary.** ESPN is the free fallback for its leagues, but its CFL scoreboard is deliberately empty (`validate_sport` keys off that), so when the quota ran out on 2026-08-08 every CFL pick recorded `no_game`: the free `/events` endpoint still matched the game (which is why those entries carry a correct `game_date`) while the market call 401'd into an empty bookmakers list. `_fetch_bovada_bookmakers` scrapes Bovada's public coupon JSON (no auth, no quota, works from the VPS) and shapes it into an Odds-API bookmakers list, so `lookup_pick_odds`, the proximity/gap guards and `_betonline_both_sides` (Bovada is already in `_BOOK_PRIORITY`) consume it unchanged. It fires only when the Odds API produced no usable price. Three shaping rules, each load-bearing: **alternate lines fold into the main market key** (`spreads_h1`, not `alternate_spreads_h1` — `_lookup_spread` only reads `alternate_*` where the Odds API sells it, so an API-style key would make Bovada's 1H alternates invisible; for a one-book source main-vs-alternate is a request artifact, not a fact about the price); **player props must not leak into totals** (the totals branch matches bare "Total"/"Total Points" only — "Total Passing Yards - X" would otherwise price a game total); and **the pregame caller refuses a started event** (`_bovada_pick_event(allow_started=False)`) because post-kickoff the coupon serves live prices and nothing downstream could tell one recorded under a pregame match_type from a closing line — the started branch passes `allow_started=True` and gets the honest `live_` prefix instead, with `pregame_*` left None (Bovada has no history; note near kickoff the coupon also trims to game lines only, so 1H/1Q markets are a pregame-window thing). Adding a league = one line in `_BOVADA_PATHS` plus a check of `_BOVADA_TEAM_ALIASES` (Bovada says "British Columbia Lions"; the parse and Odds API say "BC Lions" — `_team_matches` cannot bridge that pair). `scripts/test_bovada_fallback.py` pins the shaper and every market family offline against the real captured coupon.

**A line the pick matched but that already states the capper's price IS the pick's line — stop there.** `_insert_odds` (`tracker_format.py`) skips such a line so we don't restate a number the capper already showed, and `_place` returns `None` for it. That `None` means "keep looking" for a line carrying a *different* pick's tag, but applying it to the source-price case walks the loop past the bet and onto whatever later line also matches — the write-up that names the team again, or an angle record holding the bet's number ("9-3 off 2 wins" for an over 9). The price then renders after the analysis instead of on the bet. Any such line resolves through the `src_declined` rule instead: quiet inside the 3% move threshold, `[-190 now]` on the pick line beyond it. `scripts/test_source_priced_odds.py` pins both halves offline (fall-through *and* later-line match). Note the sibling trap when repairing these live: the tracker rebuilds each edit from the current message text, so a tag on the wrong line must be **stripped**, not just re-placed.

**Blockquotes are angle records, never the bet — no placement pass may tag one, and the two matchers must not drift.** Second entry into the same landing spot as above, from the *unmatched* side: "Dbacks ML" parses to "Arizona Diamondbacks" (no word shared with the message), a bare ML has no line number, and stripping team words from the description leaves just "ml", so every `_insert_odds` pass failed and the pick fell to `_best_content_line`, whose last-content-line rule put `[-141]` on the "35-10 off 1 loss" blockquote — three times before it was caught (3639, its fan-out sibling, and 3485 a month earlier, deleted at source before anyone saw it). The emoji matcher (`_match_pick_line`) was immune all along: it excludes blockquote lines and carries a bet-line-heuristic pass (`\bml\b`, odds/spread/total shapes) that `_insert_odds` lacked, so odds and emoji disagreed about which line is the pick. Both now share `_blockquote_lines()` and both cascades end in the same heuristic — **when touching either matcher, change the other or make them share the code**. `scripts/test_odds_blockquote_placement.py` pins the class offline; for anything touching placement, the real regression net is the corpus replay (old-vs-new `_insert_odds` over every priced `parse_cache` entry with tags stripped) — it must show only the diffs you intended.

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
python scripts/backfill_capper.py --account boyerBets_              # since = the odds horizon
python scripts/backfill_capper.py --account boyerBets_ --since 2026-01-01
```

**`--since` defaults to as far back as we can still PRICE, and that date is probed, not hardcoded.** `scores.espn_odds_horizon()` walks back a month at a time until two consecutive months have games but no closing odds, then caches the answer for a week (`~/.espn_odds_horizon.json`, `refresh=True` to force). It returned `2025-12-01` on 2026-08-09, matching the hand-measured edge exactly (present at 2025-12-10, gone by 2025-10-12). Probing matters because the horizon **slides with the calendar** — a constant would be silently wrong within months. It samples only leagues actually in season that month (`_HORIZON_SPORTS_BY_MONTH`); an out-of-season league returns an empty scoreboard, which means *no games*, not *no odds*, and would report a far shorter horizon than the truth. Fetching tweets older than the horizon only adds rows the export can settle at `-110`.

Stages, each persisting to `scripts/output/<account>_*.csv`:
`fetch_x_posts` → `parse_posts_csv` → `grade_csv` → `format_graded_csv` → `sheets_export`

Because every stage persists, re-run only what you need instead of paying for the parse again (full parse of ~600 tweets ≈ $6; grading ~100 picks ≈ $0.50):

**Backfills price from ESPN, and cost zero Odds API quota.** `format_graded_csv.py` resolves each row's price in order: the tweet text → **ESPN's closing line** (`scores.espn_closing_odds`) → `-110`. The Odds API is **opt-in** (`--use-odds-api`) because its historical endpoint is what drained the month on 2026-08-08. ESPN's summary `pickcenter` carries a book's explicit **open/close** for moneyline, spread (line + juice) and total — no key, no quota, and the ESPN client and scoreboard cache already exist.

Two limits, both measured, neither a reason to avoid it:
- **It is one book's close, not best-of-eleven.** Against 62 picks the Odds API had priced exactly, ESPN runs a median **1.37 points of implied probability worse** for the bettor (worse on 50/62, identical on 6, within 2pp on 41). That is the expected gap between DraftKings' close and a best-price search — and for a capper's *historical record* it is arguably the fairer basis. It is still money left on the table for a live pick, which is why **nothing in the tracker calls this.**
- **Coverage is ~70%**, and the misses are honest ones: pickcenter has no period markets and no alternate lines, so an MLB `-1` run line finds only ESPN's standard `-1.5` and is **refused** rather than mispriced (a grade decided at the pick's line must not be paid at a different one). Those fall to `-110`. The run summary counts `text / ESPN / Odds API / defaulted` **explicitly, never as a residual** — deriving one from the others folds the `-110` defaults into "from text" and a coverage gap then reads as full coverage.
- **Odds history reaches back ~8–9 months, and that horizon MOVES.** Sampled 2026-08-09: **100% of 64 games from 2026-01 through 2026-08** had pickcenter with a closing ML and total (every sport), still present at 2025-12, **gone by 2025-10** and every older date tried (2025-07, 2024-07, 2023-12). A 2022 NFL game had pickcenter but no `close` block — which the code refuses rather than fabricating. So a `--since 2026-01-01` backfill is fully covered *today* and the same command will not be in a year. Because a missing price silently becomes `-110`, `_warn_thin_odds_coverage` flags a run whose sheet is mostly invented. Its thresholds are **calibrated to the measured ~30% normal default rate** — 50% overall, or 25% when ESPN scored zero (source dead, not merely patchy). Do not lower them to "be safe": a warning that fires on every healthy run is one nobody reads.

> ⚠️ **If you do re-enable `--use-odds-api`, the odds lookups can cost a whole month of quota — budget it before running.** `format_graded_csv.py` calls `fetch_odds` (the **historical** entry point) once per pick, and historical costs **10 per region per market** vs 1 for current. Measured against the live cache: historical requests average **48 credits** each, current ones **4** — so a few hundred backfilled picks is 15,000–20,000 credits against a 20,000/month plan. This is what exhausted the quota on 2026-08-08, and it leaves **no server-side trace** when run locally, which is why the VPS logs only accounted for ~3,300 of the 20,000. Quota resets on the **subscription anniversary** (the day of the month the plan started), not the 1st.
>
> Cost is charged on **markets RETURNED × regions SPECIFIED**. Those halves behave differently and it matters: asking for a market that doesn't exist for a sport is genuinely free (hockey period markets on an NBA game cost nothing), but asking for one that *does* exist and is never read is billed in full — which is what the old shotgun lists did, at a measured **2.59 markets returned per request** for picks that read 1–2. Regions are billed as specified, so they are a flat multiplier.

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
- **`user_tweets` is not scoped to that user — filter on `tw.user.username`.** twscrape parses a timeline by flattening **every** Tweet object in the GraphQL response (`to_old_rep` walks it recursively), so a tweet Trent *quotes* is yielded as its own top-level item authored by whoever wrote it. Retweets are dropped by id (`retweeted_ids`); quotes are not, and `_is_retweet` can't catch them either since quoted text has no `RT @` prefix. Unfiltered, someone else's pick becomes Trent's: on 2026-08-08 he quote-tweeted @krabs_bookit's "Outlaws -1.5" to fade it, and the channel ended up holding **both sides of the same game**, which grade to opposite verdicts into one record. Nothing downstream can flag this — the URL is built from the *polled* handle and x.com **307s a wrong handle to the real author**, so a fabricated link resolves and the pick prices and grades like any other. Build the URL from `tw.user.username`, and filter **before** the date check: a quoted tweet is usually older than the lookback, and letting it feed `old_streak` trips the "3 consecutive old tweets" break and silently truncates the scan over real picks behind it. `scripts/test_trent_author_filter.py` pins both halves offline. To audit provenance cheaply, `curl -sI x.com/<handle>/status/<id>` and read the `location:` header — free, no auth, authoritative.
- **Singles only.** This channel forwards SINGLE-GAME bets; multi-leg parlays are not sent. Three checks enforce it, and the order matters: `is_pick_text` decides *whether* it's a pick, `is_pick_image` is a fallback that can only rescue a tweet the text rejected, and `is_parlay_image` is a **veto** that runs on every candidate with a photo — *including* ones the text already approved. That last one exists because the tweet text often names just one leg of the slip ("Swapped Braves ML for Red Sox ML" attached to a 5-leg FUGAZI FIVE), so the image is the only ground truth about what the bet actually is. Keep the veto's prompt default-to-pass: a false positive silently stops real picks, and nothing downstream would flag it.

  **The veto describes the SHAPE of a multi-leg ticket, never the labels a book prints.** Pikkit draws every multi-leg ticket identically — a header, then one card per leg — and only the header wording changes, so a prompt listing the labels it had seen ("N-LEG PARLAY", "PARLAY", "SGP", …) put the whole judgement on a string Pikkit chose. A 6-leg same-game HR-under ticket headed **"6-Pick Entry"** was forwarded as a single (msg 53). The split is deterministic — replayed on the real slips the old prompt vetoed the "2-Leg Parlay" **6/6** and the "6-Pick Entry" **0/6** — because two of its own rules land an unlisted header on false: per-leg cards read as the "several separate slips" exclusion, and "anything you are not sure about" turns hesitation into a forward. So the label list wasn't one rule among several, it *was* the decision, and default-to-pass (correct — a false positive silently kills real picks) is what makes a whitelist dangerous rather than merely incomplete: **an unrecognised header doesn't fail loudly, it ships the parlay.** Adding "N-Pick Entry" holds only until the next rename. The fix is shape: a header that *counts* selections, or 2+ selections sharing one stake/price/payout, explicitly including same-game and one-card-per-leg (6/6 on both tickets, 0 false positives across the 10 previously-forwarded singles). `scripts/test_parlay_veto.py` pins both headers against committed slips — it is the one `test_*.py` here that is **not** offline (the veto *is* a Claude call, ~$0.02/run), and both negatives are in it because tightening this prompt is what silently kills real picks.

  **Fixture text must be the tweet's exact `rawContent`.** Hand-typing it — an invented `t.co` token, two dropped trailing spaces — moved the *old* prompt's verdicts on both slips and manufactured an apparent nondeterminism I nearly recorded as the root cause ("borderline classifier" rather than "deterministic format gap"), which argues for a different fix. Same trap as replaying a classifier through a rebuilt input anywhere else here: pull the input from the source object, never retype it.

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
