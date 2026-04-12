import json
import os

from scores import ESPN_LEAGUES, fetch_espn, odds_requests_used
from ai import (
    claude_parse,
    claude_grade,
    build_context,
    CONTEXT_SKIP,
    CONTEXT_ESPN_ERROR,
    CONTEXT_PENDING,
    usage_cost,
    fmt_cost,
)
from tracker_format import msg_plain_text, extract_label, strip_label
from tracker_grading import grade_matches_label


def _skip_reason(r: dict) -> str:
    if r.get("is_parlay_leg") and r["sport"] not in ESPN_LEAGUES and r["sport"] != "Tennis":
        return f"parlay (no data: {r['sport']})"
    if r["sport"] not in ESPN_LEAGUES and r["sport"] != "Tennis":
        return f"no data ({r['sport']})"
    if r["bet_type"] == "prop":
        return "prop"
    return "unknown"


def _write_detail_file(path: str, source: str, results: list, graded: list,
                       correct_list: list, skipped_list: list, wrong_list: list,
                       cost: float) -> None:
    sep = "=" * 80
    thin = "-" * 80

    with open(path, "w", encoding="utf-8") as f:
        def w(line: str = "") -> None:
            f.write(line + "\n")

        # Header
        w(sep)
        w(f"BACKTEST DETAIL REPORT")
        w(f"Source : {source}")
        w(f"Total  : {len(results)}  |  Graded: {len(graded)}  |  Skipped: {len(skipped_list)}")
        if graded:
            pct = round(100 * len(correct_list) / len(graded))
            w(f"Accuracy: {len(correct_list)}/{len(graded)} ({pct}%)")
        w(f"[Claude total] {fmt_cost(cost)}")
        w(sep)

        for r in results:
            mark = "OK" if r["correct"] else ("--" if r["skipped"] else "XX")
            w()
            w(sep)
            w(f"[{mark}] MSG {r['msg_id']}  |  {r['date']}  |  {r['sport']}  |  label={r['label'].upper()}  grade={r['grade']}")
            w(sep)

            # Raw message text
            w("RAW TEXT:")
            for line in r["raw_text"].splitlines():
                w(f"  {line}")
            w()

            # Parsed pick fields
            w("PARSED PICK:")
            p = r["parsed"]
            w(f"  description : {p.get('description', '')}")
            w(f"  bet_type    : {p.get('bet_type', '')}")
            w(f"  period      : {p.get('period', 'game')}")
            w(f"  teams       : {p.get('teams', [])}")
            w(f"  player      : {p.get('player', '')}")
            w(f"  prop_stat   : {p.get('prop_stat', '')}")
            w(f"  line        : {p.get('line', '')}")
            w(f"  direction   : {p.get('direction', '')}")
            w()

            # Context passed to grader
            w("CONTEXT (sent to grader):")
            ctx = r["context"]
            if ctx == CONTEXT_SKIP:
                w("  [skipped — no grader call]")
            else:
                for line in ctx.splitlines():
                    w(f"  {line}")
            w()

            # Grader output
            w(f"GRADE: {r['grade']}")
            if r["calc"]:
                w(f"CALC : {r['calc']}")
            if r["skipped"]:
                w(f"SKIP REASON: {_skip_reason(r)}")
            w(thin)

        # Incorrect summary
        w()
        w(sep)
        w("INCORRECT GRADES:")
        w(sep)
        if wrong_list:
            for r in wrong_list:
                w(f"  msg {r['msg_id']:3d}  {r['date']}  {r['sport']:6s}  got={r['grade']}  expected={r['label'].upper()}")
                w(f"    pick   : {r['pick']}")
                w(f"    calc   : {r['calc']}")
                w()
        else:
            w("  (none)")


async def run_backtest(filepath: str) -> None:
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    channel_name = data.get("name", filepath)
    messages = [m for m in data["messages"] if m.get("type") == "message"]

    print(f"\nBacktest: {channel_name}  ({len(messages)} messages)")
    print("=" * 72)

    results = []
    scoreboard_cache: dict[tuple[str, str], dict | None] = {}
    summary_cache: dict[tuple[str, str], dict | None] = {}

    for msg in messages:
        plain = msg_plain_text(msg)
        if not plain.strip():
            continue
        # Skip messages whose first line contains "__" (manually excluded)
        if "__" in plain.splitlines()[0]:
            continue

        label = extract_label(plain)
        if label is None:
            continue

        clean = strip_label(plain)
        date = msg["date"][:10]

        parsed = await claude_parse(clean, date)
        if not parsed:
            print(f"  [parse fail] msg {msg['id']}")
            continue

        sport = parsed.get("sport", "Other")
        picks = parsed.get("picks", [])
        if not picks:
            continue

        # Fetch scoreboard once per (sport, date)
        sb_key = (sport, date)
        if sb_key not in scoreboard_cache:
            scoreboard_cache[sb_key] = await fetch_espn(sport, date)
        scoreboard = scoreboard_cache[sb_key]

        for pick in picks:
            pick_desc = pick.get("description", clean[:80])
            bet_type = pick.get("bet_type", "")
            period = pick.get("period", "game")
            is_parlay_leg = pick.get("is_parlay_leg", False)
            # Per-pick sport override (used for cross-sport parlays)
            pick_sport = pick.get("sport") or sport

            # Fetch scoreboard for per-pick sport if different from message sport
            if pick_sport != sport:
                pick_sb_key = (pick_sport, date)
                if pick_sb_key not in scoreboard_cache:
                    scoreboard_cache[pick_sb_key] = await fetch_espn(pick_sport, date)
                pick_scoreboard = scoreboard_cache[pick_sb_key]
            else:
                pick_scoreboard = scoreboard

            context, _game_date = await build_context(pick_sport, date, pick, pick_scoreboard, summary_cache)

            if context in (CONTEXT_SKIP, CONTEXT_ESPN_ERROR):
                grade, calc = "UNKNOWN", ""
            elif context == CONTEXT_PENDING:
                grade, calc = "PENDING", ""
            else:
                grade, calc = await claude_grade(pick_desc, date, context, bet_type, pick.get("prop_stat") or "")

            correct = grade_matches_label(grade, label)
            skipped = grade in ("PUSH", "UNKNOWN")
            mark = "OK" if correct else ("--" if skipped else "XX")

            print(
                f"  {mark}  msg {msg['id']:3d}  {date}  {pick_sport:6s}  "
                f"{grade:7s}  label={label.upper():<4s}  {pick_desc[:48]}"
            )
            results.append({
                "msg_id": msg["id"],
                "date": date,
                "sport": pick_sport,
                "label": label,
                "grade": grade,
                "calc": calc,
                "correct": correct,
                "skipped": skipped,
                "pick": pick_desc,
                "bet_type": bet_type,
                "period": period,
                "is_parlay_leg": is_parlay_leg,
                "parsed": pick,
                "context": context,
                "raw_text": msg_plain_text(msg),
            })

    # ── Summary ──
    graded = [r for r in results if not r["skipped"]]
    correct_list = [r for r in graded if r["correct"]]
    skipped_list = [r for r in results if r["skipped"]]
    wrong_list = [r for r in graded if not r["correct"]]

    print(f"\n{'=' * 72}")
    total = len(results)
    if graded:
        pct = round(100 * len(correct_list) / len(graded))
        print(f"Accuracy : {len(correct_list)}/{len(graded)} ({pct}%)  |  skipped: {len(skipped_list)}/{total}")
    else:
        print(f"Accuracy : N/A  |  skipped: {len(skipped_list)}/{total}")

    cost = usage_cost()
    print(f"[Claude total] {fmt_cost(cost)}")

    if wrong_list:
        print("\nIncorrect grades:")
        for r in wrong_list:
            print(f"  msg {r['msg_id']:3d}  {r['date']}  {r['sport']:6s}  got={r['grade']}  expected={r['label'].upper()}")
            print(f"         {r['pick'][:65]}")

    # Skip breakdown
    if skipped_list:
        skip_by_reason: dict[str, int] = {}
        for r in skipped_list:
            reason = _skip_reason(r)
            skip_by_reason[reason] = skip_by_reason.get(reason, 0) + 1

        print("\nSkipped breakdown:")
        for reason, count in sorted(skip_by_reason.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")

    # ── Write detail file ──
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(data_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(filepath))[0]
    out_path = os.path.join(data_dir, f"backtest_{base}.txt")
    _write_detail_file(out_path, filepath, results, graded, correct_list, skipped_list, wrong_list, cost)
    print(f"\nDetail file: {out_path}")
    from audit import log_api_costs
    log_api_costs("backtest", cost, odds_requests_used())
