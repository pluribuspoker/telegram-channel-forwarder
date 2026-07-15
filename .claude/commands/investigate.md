---
description: Debug and investigate issues with live data, logs, and Telegram messages
argument-hint: <issue description, Telegram link, or error message>
---

# Investigate

$ARGUMENTS

## Rules

1. **Detect your environment first.** Check if you're running locally (Windows) or on VPS (Linux, hostname `pickbot`). Use `uname -s` or check if `/home/forwarder/app` exists. This determines whether you SSH or run commands directly.
2. **Live data is on the VPS.** If local, SSH to VPS. If on VPS, run commands directly as forwarder.
3. **Telegram messages are accessible.** Use `scripts/vps_msg.py` on VPS. If local, run via SSH. If on VPS, run directly.
4. **Don't give up after one failed attempt.** Try variations before concluding data doesn't exist.
5. **Use a git worktree for code changes** (see CLAUDE.md worktree pattern). Do ALL commits in the worktree before merging.

## Workflow

1. **Detect environment**: Check if running on VPS or locally
2. **Gather context**: Fetch the Telegram message if linked, check parse_cache and/or DB
3. **Read the relevant code** before theorizing
4. **Verify VPS matches local** (if local): `ssh root@209.38.51.86 'cd /home/forwarder/app && git log --oneline -1'`
5. **Check VPS logs** (`journalctl -u telegram-tracker`, `journalctl -u grade-daemon`, etc.)
6. **Identify root cause**: Trace the full pipeline before fixing
7. **Fix and verify** with the real data that triggered the bug (replay actual inputs through the fixed code)
8. **Deploy code fix first** (push + deploy) before touching live data
9. **Fix live data** if needed (wrong emoji, DB entry, etc.). Use Bot API with `parse_mode: "HTML"` — check `msg.media`: use `editMessageCaption` for photo/video, `editMessageText` for plain text
10. **Run tracker manually** scoped to the affected channel for instant verification

## VPS queries

**If running on VPS** (e.g., via Telegram channels):
```bash
cd ~/app && ~/venv/bin/python scripts/vps_msg.py <channel_id> <msg_id>
cd ~/app && ~/venv/bin/python scripts/vps_grades.py --msg-id <id>
cd ~/app && ~/venv/bin/python scripts/vps_grades.py --search <term>
cd ~/app && ~/venv/bin/python tracker.py --live --channel <channel_id>
```

**If running locally** (SSH into VPS):
```bash
ssh root@209.38.51.86 'su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/vps_msg.py <channel_id> <msg_id>"'
ssh root@209.38.51.86 'su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/vps_grades.py --search <term>"'
ssh root@209.38.51.86 'su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --channel <channel_id>"'
```

## Lessons

- Never inline Python in SSH commands beyond simple one-liners. Write a temp script, `scp` it, run it.
- For grading bugs, trace the ENTIRE pipeline (`claude_parse` → post-parse → `validate_sport` → `build_context` → fetcher → `claude_grade`) before fixing.
- Always use `--channel <id>` when running `tracker.py --live` locally — unscoped runs broadcast to production.
- **Log queries: mind the timezone.** `journalctl --since/--until` uses the VPS **local time (America/New_York, EDT)**, but `graded_at` (DB/cache) and Telegram `ts` are **UTC**. Subtract 4h (UTC→EDT) or the query returns "-- No entries --" and you'll wrongly conclude a service was idle. Confirm with `timedatectl`.
- **`systemctl is-active` lies about hangs.** A wedged process (e.g. blocked on an untimed network call) still shows `active`. Verify liveness by the recency of its last log line, or `systemctl show <svc> -p WatchdogTimestamp` — not `is-active` alone.
- **Broadcasting is daemon-only.** The tracker grades + edits emojis but must never set `broadcasted=True` (it doesn't broadcast). If a result got its emoji but no broadcast, suspect the daemon was down when the tracker graded it — check the daemon's log continuity around the grade time.
- **`send_as_user` channels are NOT bot-editable.** Their messages are sent by the Telethon userbot, so the Bot API returns `400: message can't be edited`. Only the tracker can edit them (via its `_user_edit_message` Telethon fallback for `user_edit_channels`). The grade daemon (Bot-API-only) skips these channels entirely. If a pick in such a channel graded but shows no emoji, it's this class of bug — check for `edit failed <ch>:<msg>` in the daemon log.
- **Don't trust cached `html_text` to decide if a pick is "ungraded".** Old cache entries often have empty/stale `html_text`, so scanning the cache for "no emoji" produces false positives. Verify against the **live** Telegram message text (`vps_msg.py` / `iter_messages`) before concluding a pick is stuck.
- **`vps_msg.py`/Telethon need the `-100`-prefixed channel ID.** A `t.me/c/<short>/<msg>` link gives the short ID (e.g. `4394797084`); passing it bare fails with `Could not find the input entity for PeerUser(...)`. Prepend `-100` → `-1004394797084`. Same for any `get_messages`/`iter_messages` call.
- **Result emoji on the wrong line usually means `_match_pick_line` returned `None`.** The fallback in `_insert_emojis` (`tracker_format.py`) then appends the emoji to the *last content line*, which for forwarded tweets is often trailing emoji/flag spam. Root-cause it in `_match_pick_line`, not the fallback. Team-level props (BTTS, clean sheet) were a blind spot: teams live on a header line and the prop keyword on a separate pick line, so the team+prop combined match (Pass 1) matched neither — fixed by Pass 1b (match on `prop_stat` alone, dd34f4b).
- **To force a re-parse, invalidate ALL sibling cache entries sharing `_source_key`, not just one.** A forwarded pick appears in multiple channels; each channel's entry (`<ch>:<msg>`) is a mirror of one source (`_source_key`, e.g. `1910823870:426471`). The tracker re-populates an invalidated entry by copying `parsed`+`odds` from any sibling that still has it (`[mirror] reusing parse+odds from …`). Delete `parsed`/`odds_by_pick`/`leg_verdicts` from **every** entry with the same `_source_key` in one write, keeping `_forwarded` (so it won't re-forward), then re-run the tracker per channel.
- **A pick stuck as `sport: Other` with `odds_match_type: sport_unsupported(Other)` usually means the sport isn't wired in, not a parse bug.** Adding a sport = enum in `ai.py` + `ESPN_LEAGUES` (`scores.py`) + `SPORT_KEYS`/`PROP_STAT_MARKETS`/`HALF_POINT_COST` (`odds.py`) + tracker parlay-validation whitelist. See [[wnba-support]].
- **When manually re-grading, stop the grade daemon first (or let it grade).** The tracker and daemon share `parse_cache.json`; the tracker loads it once and rewrites the whole dict, so a manual `tracker.py --live` run overlapping a live daemon broadcast could clobber the daemon's freshly-set `broadcasted=True` and cause a duplicate broadcast. Hardened in `_save_pending_cache` (commit 5e6ef28 merges on-disk broadcasted flags on write), but stopping the daemon during manual re-grades is still the safe habit. See [[parse-cache-broadcast-race]].
- **"Nothing forwarded/graded since date X" → suspect a dead service, not a logic bug.** Check `journalctl -u <svc>` for `status=203/EXEC`: systemd couldn't exec the `ExecStart` target because the script lost its executable bit. Git tracks the mode — a script committed as `100644` (not `100755`) lands non-executable on the next checkout/deploy and every timer run fails instantly. Diagnose with `git ls-files -s <script>`; fix with `chmod +x` (live) **and** `git update-index --chmod=+x <script>` + commit (durable), else the next deploy re-breaks it. Bit Trent watcher Jul 12–14 (b7a4131).
- **A pick logged as UNKNOWN "no picks extracted" (sport set, `picks: []`) is a parse-prompt gap, not a service/data bug.** Claude classified the sport but couldn't extract a bet — usually because the message is pure slang with no explicit bet type/line (e.g. "i'm nuking the AL for my coin back" → moneyline on AL All-Stars, f702997). Reproduce with `claude_parse` (deterministic here), fix the rule in `_PARSE_PROMPT` (`ai.py`), then re-verify with a control case so the new rule doesn't extract picks from plain commentary. Invalidate the `{"_failed": true}` cache entry (delete the `<ch>:<msg>` key) before re-running the tracker, or it stays notified/skipped.
- **Odds fetched (`odds:N/N` in tracker output) but `edit:0` and no odds tag on the message = the pick line couldn't be matched to the message text.** Same root class as emoji-on-wrong-line: `_insert_odds`/`_insert_emojis` anchor by team name / description / bet-number overlap, which fails for pure-slang tweets (no team name in the text, e.g. "nuking the AL"). Both now share `_best_content_line()` (`tracker_format.py`) as a single-standalone-pick fallback that places the tag on the last real content line (skipping capper name, blank lines, and `🔗 View on X` attribution links). If odds/emoji still land wrong, fix the shared helper — don't duplicate matching logic between the two inserters (they drifted before: odds had no fallback at all, 4d09cd2).
- **To reproduce a parse/grade call on the VPS, run from `~/app` with dotenv loaded.** The anthropic client throws `TypeError: Could not resolve authentication method` unless `ANTHROPIC_API_KEY` is in env — a bare script doesn't inherit it. Prepend `from dotenv import load_dotenv; load_dotenv('.env'); load_dotenv('.env.local')` and put the temp script inside `~/app` (not `/tmp`) so `import ai`/`tracker` resolves. Monkeypatch `ai._PARSE_PROMPT` in the script to test a prompt tweak before editing the file.
- **Wrong odds on a knockout-soccer moneyline = the h2h (90-min) price shown instead of the to_qualify (advance) price.** For knockout ties, The Odds API `h2h`/`h2h_3_way` is the 90-minute 3-way line (draw is a separate outcome), while `to_qualify` is the team-advances price (incl. ET + penalties) — they differ hugely (England +172 on 90-min vs -126 to advance vs Argentina). `_ADVANCE_RE` (`odds.py`, moneyline-only) routes to `to_qualify` when the pick **description** contains advancement wording. **Don't chase this with a slang-phrase list in the regex** (the first attempt broadened it to "coming home"/"lift the trophy"/… — brittle, unbounded). The durable fix is upstream: `claude_parse` is **image-aware** — when the message text is pure slang (no explicit bet signal, `tracker._is_text_thin`) and a bet slip photo is attached, the tracker re-parses with the image as ground truth, so the slip's own market ("England advances / Game Winner") becomes the description. `_ADVANCE_RE` then just matches the canonical `advanc*`/`qualif*` stems the parser emits. So: a wrong knockout-ML price means either the parse missed the market (check whether the slip image was consulted — is the text thin and was there a photo?) or the description wording isn't a stem the regex covers. Don't trust odds read off the slip by Claude (it misread 1.78x as "+172"); only take the *market* from the image and fetch the price from The Odds API.
- **"Should've been N separate picks but graded as one parlay" (or vice-versa) is a slip-resolved *structural* fact, not a text-parse tweak.** A vertically-listed "card" (e.g. "Official World Cup Card: England to advance / BTTS / Over 2.5 … entire balance deployed on this game") reads like a parlay, so the text-only parse marks every leg `is_parlay_leg=true` — but the slip showed three separate "1-Pick" tickets, each its own stake/payout = 3 straight bets. The image is ground truth for separate-vs-combined, same as it is for the exact market (see [[prefer-groundtruth-over-heuristics]]). The image re-parse fires not only for thin/slang text (`_is_text_thin`) but ALSO when the parse *inferred* a multi-leg parlay while the text never says so (`tracker._parlay_structure_uncertain`: ≥2 `is_parlay_leg` legs AND no "parlay/teaser/sgp" word / spaced `+`/`&`). The image prompt in `claude_parse` decides structure from the slip (separate stakes/"1-Pick" ⇒ separate, `is_parlay_leg=false`; one combined stake+payout ⇒ parlay). Explicit-parlay text still trusts the text and pays no image cost (fd896c9). To fix a mis-grouped live pick: deploy, then invalidate the `<ch>:<msg>` cache entry (drop `parsed`/`odds_by_pick`/`leg_verdicts`) and re-run the tracker scoped to the channel — verify with a control that a real "+"/"parlay" message still stays a parlay.
- **`_insert_odds`/`_insert_emojis` are idempotent — they SKIP any line that already carries a tag (`_ODDS_TAG_RE`), so re-running the tracker will NOT overwrite a *wrong* odds tag already on the message.** To correct a bad odds/emoji tag on a live message you must either strip the old tag from the message first (then re-run) or edit the message directly. When editing directly, also update `odds_by_pick` in the cache entry to the corrected value so the next tracker run stays consistent and doesn't re-fetch. A photo/video message needs `editMessageCaption` (not `editMessageText`); pass the cached `html_text` with `parse_mode: "HTML"` to preserve the `🔗 View on X` link entity.

After resolving, review mistakes and add lessons above and/or to CLAUDE.md. Save relevant feedback to memory. If a lesson describes a code bug, fix the code instead of adding the lesson — lessons should document unavoidable constraints, not workarounds.
