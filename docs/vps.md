# VPS, services, Claude channels, environment files

> Full reference moved out of CLAUDE.md (terse rules live there). This file is read on demand — keep the complete detail and incident history HERE, not in CLAUDE.md.

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

