# Capper backfill, Twitter/X parsing, CSV grading

> Full reference moved out of CLAUDE.md (terse rules live there). This file is read on demand — keep the complete detail and incident history HERE, not in CLAUDE.md.

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

