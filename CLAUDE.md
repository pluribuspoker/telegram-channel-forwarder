# Telegram Channel Forwarder

Rules here are terse on purpose. Each section points to a `docs/*.md` file holding the full detail and incident history — **read that doc before working on its subsystem**, and add new deep detail there, not here.

## Preferences

- Chain related shell steps with `&&` on one line.
- Parallel tasks use git worktrees: `git worktree add ../telegram-forwarder-<slug> -b <branch>`, commit+push there, merge from the main repo dir, `git worktree remove` after.

## Telegram message formatting

- Always pass `msg.entities` through via `formatting_entities=`; never rebuild text without them. `text_suffix` in `send_group` appends without dropping entities.
- **Entities pair with `raw_text`, never `.text`** — `.text` is the markdown render (literal `**`), so pairing it with `formatting_entities=` shifts every entity and leaves stray delimiters. Whatever string goes beside `formatting_entities` must be `raw_text`. (`enrich_caption` keeps `.text` deliberately — that caption is markdown-parsed on send.)
- All content matching (`filter_pattern`, `reply_chain_cappers`, …) must run on `.raw_text` — against `.text` an anchored `^Tony POD` sees `**Tony POD**` and the pick drops with no error.
- Pinned by `scripts/test_forward_entities_regression.py`.
- **Any edit rebuilt from HTML must use `to_bot_html`/`patch_expandable_blockquotes` (`tracker_format.py`)** — stock Telethon drops the blockquote `collapsed` flag both ways (unparse AND parse), so one bare-`<blockquote>` edit expands a collapsed quote unrecoverably (the flag survives only at the source). Bot API syntax: `<blockquote expandable>`. Pinned by `scripts/test_expandable_blockquote.py`.

## VPS — docs/vps.md

- **Reserved IP `209.38.51.86`** (never the droplet IP). `ssh root@209.38.51.86`. Aliases (interactive SSH only, `/root/.server_aliases.sh`): `flogs tlogs logs start stop restart status deploy grade gradetest`.
- **Deploy cautiously** — rapid session restarts trigger Telegram flood waits. Push+deploy only verified fixes; otherwise let the user deploy.
- Test mode: locally `python listener.py --test`; on VPS `stop` first, run as forwarder, then `start`. Uses `test_source_channel`→`test_dest_channel`, bypasses `filter_pattern`.
- Log colors come from `_fmtlog` in `/root/.server_aliases.sh` — never add ANSI codes to Python prints.

### Services (full detail: docs/vps.md)

- `telegram-forwarder.service` — listener (persistent).
- `telegram-tracker.timer` — pick grader, every 5 min (Telegram reads, parsing, odds).
- `grade-daemon.service` — grades every 10s, **sole broadcaster**, zero Telethon; cycle timeout + systemd watchdog. Shares `parse_cache.json` with the tracker (atomic `os.replace`); sets `broadcasted=True` so the tracker skips.
- `angles-dashboard.service` — serves `https://fightclubpicks.cc`.
- Watchdog timers, all silent-unless-alerting via `WATCHDOG_BOT_TOKEN` DMs: `mem-watchdog` (OOM/swap; VPS has a 2GB swapfile), `claude-spend-watchdog` (spend ledger written at the `ai.py` choke point → `logs/claude_spend.jsonl`; journald is fallback only, never summed), `odds-quota-watchdog` (alerts on state CHANGE only, ✅ on recovery; meters via the `x-requests-last` header, never by differencing `remaining`), `claude-auth-watchdog` (probes `api.anthropic.com` directly — must never shell out to `claude`; only 401/403 counts as dead).
- `claude-watchdog-bot.service` — interactive: `/mem /status /restart /kill /logs /tmux /auth /reauth /authcode`.
- **Ubuntu-update notifications** (`deploy/sbin/` → `/usr/local/sbin/`, watchdog chat, "pickbot:" voice): `update-notify` (ExecStartPost drop-in on `apt-daily-upgrade.service`) announces each unattended-upgrades run that changed packages — needrestart bounces affected services (incl. the Claude session, sometimes twice) in the ~6:31 AM window with no reboot; `auto-reboot.timer`→`reboot-if-required` (daily 06:00, only if `/var/run/reboot-required`) + `boot-notify.service` cover reboot-class updates. Restart pings stay bare by design — the cause arrives via this family (docs/vps.md).

### Claude Code via Telegram (full detail: docs/vps.md)

- Runs in tmux session `claude` as forwarder; user DMs `@ForwarderClaudeBot`. Attach: `tmux attach -t claude`. Restart: `su - forwarder -c "tmux kill-session -t claude; tmux new-session -d -s claude 'cd ~/app && claude --channels plugin:telegram@claude-plugins-official --dangerously-skip-permissions --model claude-fable-5 --effort max'"`
- `/clear` in Telegram does NOT reset context — only restarting the `claude` process does.
- Auth: 1-year `CLAUDE_CODE_OAUTH_TOKEN` in `~/.claude/auth.env` (not `.credentials.json`). Phone re-auth: `/reauth` → `/authcode <code>` in the watchdog bot; the `setup-token` tmux needs its dedicated socket (`-L authtok`) and wide pane (`-x 400`) — both load-bearing (docs/vps.md).
- **Investigate trigger:** a message starting `inv `, or reporting a pick/grading problem, or asking why something did/didn't happen (esp. with a `t.me/...` link) → invoke the **investigate skill** (real Skill call). Don't answer ad-hoc.
- On VPS (hostname `pickbot`): run commands directly; `sudo -n systemctl ...` (bare aliases are interactive-SSH-only); git works from `~/app`.
- 👀 reaction = harness received the message (UserPromptSubmit hook). No 👀 after a few seconds = dropped, resend. The "▶️ Restarted" hook message does NOT prove the receive loop is up.

### Environment files (full detail: docs/vps.md)

| File | Synced | Contains |
|---|---|---|
| `.env` | ✅ `syncenv` | config present on both machines |
| `.env.local` | ❌ | `TELEGRAM_SESSION`, `BOT_SESSION`, `X_AUTH_TOKEN`, `X_CT0`, `PIKKIT_TOKEN` |
| `.env.guesser` | ❌ server only | `ODDS_API_KEY` override for `nfl-lines-fetcher.service` |

- **Any server-only value belongs in `.env.local`** — `syncenv` overwrites the server `.env` and silently deletes keys absent locally (took the Trent watcher down 2 days). `.env.local` loads after `.env` and wins.
- Regex in `MAPPINGS_CONFIG` needs **four** backslashes (`\d+` → `\\\\d+`).
- Sessions: `python scripts/get_session.py` / `get_bot_session.py` — run on the VPS for VPS sessions. After writing `.env.local` there: `chmod 600` + `chown forwarder:forwarder`.

## Pick tracker — docs/tracker.md

- `grade` alias = `--days 1`; older picks: `tracker.py --live --days 2`. Targeted: `--target=-100…:ID` (use `=`, repeatable, skips the date scan).
- Fast path: listener enqueues exact targets ~3s after a forward; summary line in `journalctl -u telegram-forwarder`, full output `logs/tracker_quick.log`.
- UNKNOWN is never persisted; ungradeable legs are capped by `should_skip_unknown()`/`record_unknown_attempt()` (`common.py`, shared by daemon+tracker) — 6 tries, 30-min spacing, verdict-less dict with deliberately no `game_date`.
- Player props: `box_score_text` emits **every** stat group (batting+pitching, passing+rushing, …) as generic `key=value`; prop context includes line scores (NRFI). Player absent from the bound game → slate-wide strict-name box rescue (stale roster memory binds a traded player's OLD team); parse consults the slip image when a player prop's team isn't in the text — the slip's team chip beats roster memory. Test: `scripts/test_box_score_props.py`.
- `audit.record` fingerprints the rendered post and skips identical reposts. The daemon retires entries unresolved 3+ days past the latest **game** date (`GRADE_STALE_UNRESOLVED_DAYS`) — reuses `_failed`, never broadcasts them.
- `verify_picks_on_schedule()` (`scores.py`, fresh parses only) checks every pick against the slate: confirmed / corrected (repair from a raw-message token) / suspect (reported, never auto-corrected) / unverified / n-a. **An ESPN outage must never read as a bad parse.** Adding a sport → note rival promotions in `_RIVAL_PROMOTIONS`.
- NHL regulation/3-way ML: OT = LOSS; `is_regulation_ml()` (`common.py`).
- **Scoreline bets settle by arithmetic (`try_early_grade_math`, `scores.py`), never by Claude comparing numbers.** Totals may settle mid-game (monotone) and push at final; spreads/ML at final; ANY period bet settles once its period completes (`_is_period_complete` — a finished F5/1H scoreline is frozen mid-game; halftime isn't complete until Q3 starts; baseball `1q` = 1st inning, i.e. NRFI/YRFI = total 0.5 under/over `1q`). `MATH_GRADABLE_SPORTS` is an explicit allowlist (UFC/soccer excluded on purpose); every guard fails open (`None` → Claude path). **Adding a sport = parse enum + `ESPN_LEAGUES` + `SPORT_KEYS` + `PERIOD_MAP` + check `SPORT_EXTRA_PARAMS`** — an unmapped period silently disables the math, and a default scoreboard can omit a subdivision (NCAAF default = FBS-only; `groups=90` = all D1 incl. FCS). CFL reaches this via `fetch_cfl_scoreboard()`, deliberately NOT wired into `fetch_espn`. Don't fix grading bugs with prompt text — sampled answers aren't deterministic. Tests: `scripts/test_total_grade_math.py` + full cache replay; a drop in declines is as suspicious as a disagreement.
- Cross-league nickname collisions ("Snakes") resolve via the schedule: `resolve_nickname_collision()` keyed on the RAW token (`_NICKNAME_COLLISIONS`), runs before `validate_sport`, rewrites `description`; ambiguous → flagged, never guessed.
- `validate_sport` alt-sport overrides fire only on club-level evidence (`_nickname_evidence`: nickname/short-name/abbrev token) — city words ("San"/"Los") once rebound a correctly-parsed NFL lookahead to MLB "Los Angeles Angels" and graded the wrong game. Same-sport fuzzy rescue deliberately unchanged. Test: `scripts/test_sport_override_regression.py`.
- KBO: scraped from koreabaseball.com (`fetch_kbo_context`); picks post the US evening before, so fetch `date+1`.

## Odds — docs/odds.md

- Force re-fetch: delete `odds_by_pick` from the cache entry — **only safe before first pitch**; after start, write closing lines directly via `odds._try_pregame(...)` and set `game_date` yourself (verify an already-correct sibling reproduces exactly).
- Event binding (`_find_event_id`): candidate keys merged via `_EXTRA_SPORT_KEYS` (preseason etc. = one line to add), ranked by `as_of` (the pick's date, not now), fail-closed on partial team matches. Don't fix a wrong price by widening a downstream guard. Test: `scripts/test_event_match_regression.py`.
- Requests narrowed to the pick's market+period (`_narrow_markets_for_pick`, keeps the `alternate_*` sibling — mirrored from `_alt_market_for`, pinned by `scripts/test_market_narrowing.py`) and one region (`ODDS_API_REGIONS=us`).
- **Free sources first, paid Odds API LAST resort** (sub cancelled 2026-08-22; key reverts to free 500 credits/mo): ESPN → Bovada → paid market call, in both `fetch_odds_current` and `fetch_odds`; the free `/events` match still runs first (costs 0, anchors `game_date`/`commence_time`/same-game guard). `free_only=True` skips every paid call. Test: `scripts/test_odds_retry.py`.
- **Retryable misses aren't permanent anymore**: `no_game`/`no_*_data`/`prop_not_found` re-fetched by the tracker with free sources only, every ≥30 min until game start (`should_retry_odds`, bounded by `commence_time`/`game_date`) — Bovada lists period markets (MLB F5, CFL quarters) and player props only in a pregame window the first attempt can miss. Priced/live/structural results stay final; retry misses don't re-warn the audit channel.
- **Player props price free from Bovada's prop groups** before any paid call: `(ABBR)` parenthetical required (team totals never have one), stat phrase must match EXACTLY (`_BOVADA_PROP_PHRASES`, observed payloads only — prefix rules would eat combos/milestones); unmapped stat falls to the paid path. Test: `scripts/test_bovada_props.py`.
- **Bovada is the main free fallback** (`_BOVADA_PATHS` in `odds.py`). Group allowlist `_BOVADA_LINE_GROUPS` guards against props; alternates fold into the main market key; MLB `1H` = First 5 Innings; pregame caller refuses started events (live path prefers Bovada live before any paid live call); `_bovada_result_acceptable` guards cross-source same-game. Adding a league = one line in `_BOVADA_PATHS` + check `_BOVADA_TEAM_ALIASES`. Test: `scripts/test_bovada_fallback.py`.
- A line already stating the capper's price IS the pick's line — `_insert_odds` stops there (`src_declined`; `[-190 now]` beyond the 3% move threshold). When repairing live, STRIP a misplaced tag, don't just re-place. Test: `scripts/test_source_priced_odds.py`.
- **Blockquotes are angle records, never the bet — no placement pass may tag one.** `_insert_odds` and `_match_pick_line` share `_blockquote_lines()` and end in the same bet-line heuristic — touching either matcher means changing the other or sharing the code. Same rule for link lines via shared `_is_link_line()` (URLs substring-match everything: "Boo**k**itWithTrent" ate a K-prop's emoji, a status id contains most integer lines); short prop stats (<4 chars) word-match only, and player props fall back to the bare player-name line (`_prop_stat_in_line`, `scripts/test_prop_emoji_placement.py`). Test: `scripts/test_odds_blockquote_placement.py`; real net for placement changes is the corpus replay (old-vs-new `_insert_odds` over every priced cache entry, tags stripped).
- Audit: `python scripts/audit_odds.py --days-back 7` / `--dry-run`.

## Pikkit splits — docs/odds.md

- `pikkit.py` → `get_pick_splits()` → `pikkit_by_pick`. Completed games return 403 — fetch before/during the game.
- Token: `scripts/pikkit_auth.py` **locally only** (Turnstile rejects headless), two-step SMS flow; health check `--validate`. `PIKKIT_TOKEN` lives in `.env.local` only.

## Broadcast results — docs/tracker.md

- `_format_pick` (`audit.py`) is the ONE renderer (broadcast, merged broadcast, Sheets) — **every `bet_type` branch must interpolate `period_tag`** (after team/player, before the bet). New branch = new case in `scripts/test_period_tag.py`, incl. the MLB/KBO renames (`1h`→`F5`, `1q`→`1st Inn`).
- Test workflow: `scripts/clear_emojis.py --channel <id>` (or `--days 2`) then `python tracker.py --live --channel <id>`.

## Sauce daily — docs/sauce.md

- `scripts/sauce_daily.py`: SAUCE tab → graded Pillow image (no Chromium — headless OOM'd the VPS) → channel `-1003977774560`; 6 AM ET cron (`run_sauce_daily.sh`), log `/tmp/sauce_daily_cron.log`.
- Manual: `su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/sauce_daily.py --channel -1003977774560 2>&1"`

## Capper backfill — docs/backfill.md

- One command: `python scripts/backfill_capper.py --account <X>`; stages persist to `scripts/output/`, resume with `--from`/`--only`; `--since` defaults to the probed ESPN odds horizon (`espn_odds_horizon()`, slides with the calendar).
- Prices: tweet text → ESPN closing line (`espn_closing_odds`, free) → −110. **The Odds API is opt-in (`--use-odds-api`) and can eat a whole month of quota** (historical ≈ 48 credits/request; drained the plan 2026-08-08). `_warn_thin_odds_coverage` thresholds are calibrated — don't lower them.
- Fetch runs on the VPS (X cookies live there) and auto-pauses `trent-monitor.timer` (shared X session, ~15 min UserTweets cooldown).
- Only `pod` category grades by default — `result`-category picks are revealed after winning; grading them fakes the record. The exclusion table prints every run. Never reconstruct tweet URLs from ids.
- Sheets export: one tab per account in `BACKFILL_SHEETS_ID`; workbook must be pre-shared as Editor with `forwarder@api-project-349700129720.iam.gserviceaccount.com` (Drive API disabled — the script can't create it).
- Odds sanity check flags prices outside normal bands — when it fires, check the game date first.

## Twitter/X parsing + CSV grading — docs/backfill.md

- `scripts/parse_posts_csv.py`: text parse → image parse → dedup (`--limit`, `--skip-images`). No hardcoded exclude lists — prompt rules + algorithmic dedup only.
- `scripts/grade_csv.py --account <X>` (`--sport`, `--limit`, `--categories all`). Soccer ML is 3-way: draw = LOSS (only DNB pushes); "to advance" uses final incl. ET/pens.
- `scripts/format_graded_csv.py` → sheet layout.

## Trent watcher — docs/trent.md

- `trent-monitor.timer` (15 min): @BookitWithTrent → channel `-1004394797084`. X cookies in `.env.local` ONLY; missing/rejected cookies exit non-zero and DM the operator.
- Build the API via `scripts/x_client.py build_api()`, never raw twscrape (three library-bug workarounds that otherwise present as "bad cookies" — page migrations AND build-format changes like the 2026-08 hash-length switch; tests: `scripts/test_xclid_scripts_parse.py`).
- **`user_tweets` yields quoted tweets as top-level items — filter on `tw.user.username`, BEFORE the date check**, and build URLs from it (x.com 307s a wrong handle to the real author). Test: `scripts/test_trent_author_filter.py`.
- **Singles only.** Order matters: `is_pick_text` → `is_pick_image` (rescue) → `is_parlay_image` (veto, runs on every candidate with a photo). The veto describes the SHAPE of a multi-leg ticket (counting header, or 2+ selections sharing one stake/price/payout), never a book's labels; keep it default-to-pass. Test: `scripts/test_parlay_veto.py` (NOT offline, ~$0.02/run).
- Fixture text must be byte-exact `rawContent` — never retype inputs.
- Manual: `su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/trent_watcher.py --dry-run 2>&1"` (or `--lookback 24`).

## Angle Analyzer — docs/angles.md

- `angles/extract_angles.py` → `angles/data/angles.json`; single-file dashboard `angles/index.html` served by `angles/server.py` at `https://fightclubpicks.cc` (Cloudflare → 209.38.51.86).
- Auth: Telegram membership via `/access` to `@forwarder_fc_bot` → HMAC magic link → 30-day cookie (`angles/auth.py`; handler in `listener.py`). Admin activity dashboard at `/activity`.
- Env: `ANGLES_AUTH_SECRET` (required), `ANGLES_PORT`, `ANGLES_ADMIN_IDS`, `BOT_TOKEN`.
- Manual pull: `su - forwarder -c "cd ~/app && ~/venv/bin/python angles/extract_angles.py"`

## Infra sync + runners

- `deploy/` is the source of truth for systemd units and hooks: edit there, commit, then `sudo cp` + `daemon-reload` + restart (units) or `cp` + `chmod +x` (hooks). Drift check: `bash scripts/check_deploy_sync.sh`.
- `run_*.sh` runners: **`set -o pipefail` is mandatory** (tee eats the exit code and a crash reads as success); `TimeoutStartSec` must exceed worst-case retries; set the job's `*_HEALTHCHECK_URL` (`ping_hc()` silently no-ops unset — trent and sauce are still uncovered).

## Deploy workflow

```bash
# Local
syncenv && git push
# On VPS
ssh root@209.38.51.86 'cd /home/forwarder/app && git pull && systemctl restart telegram-forwarder'
```
