"""
backfill_capper.py — One command to turn an X/Twitter capper into a graded sheet.

Runs the whole pipeline:
    fetch_x_posts -> parse_posts_csv -> grade_csv -> format_graded_csv -> sheets_export

    python scripts/backfill_capper.py --account boyerBets_ --since 2026-01-01

Every stage writes scripts/output/<account>_*.csv, so any stage can be re-run on
its own without redoing the expensive ones:

    --from grade            # reuse the existing parse, redo grade + format + export
    --only export           # just push the existing sheet CSV to Google Sheets
    --limit 20              # small slice, for trying a new account cheaply

The fetch runs on the VPS by default because the X cookies live there, and it
pauses trent-monitor.timer for the duration — both jobs share one X session and
the UserTweets endpoint has a ~15 minute cooldown, so a concurrent run means one
of them fails. The timer is restored even if the fetch crashes. Pass --local-fetch
if X_AUTH_TOKEN / X_CT0 are set locally, or run this script on the VPS itself.
"""

import argparse
import csv
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(ROOT, ".env.local"), override=True)

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")
STEPS = ["fetch", "parse", "grade", "format", "export"]

VPS_HOST = os.getenv("VPS_HOST", "root@209.38.51.86")
VPS_APP = "/home/forwarder/app"
SHARED_X_TIMER = "trent-monitor.timer"


def _hr(title: str) -> None:
    print(f"\n{'=' * 72}\n== {title}\n{'=' * 72}", flush=True)


def _run(cmd: list[str], **kw) -> None:
    """Run a local command, streaming output, raising on failure."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    print(f"$ {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
    subprocess.run(cmd, check=True, env=env, cwd=ROOT, **kw)


def _ssh(command: str, check: bool = True) -> str:
    out = subprocess.run(["ssh", VPS_HOST, command],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    if check and out.returncode != 0:
        raise RuntimeError(f"ssh failed: {command}\n{out.stderr.strip()}")
    return (out.stdout or "") + (out.stderr or "")


def _on_vps() -> bool:
    return os.path.isdir(VPS_APP) and sys.platform.startswith("linux")


# --------------------------------------------------------------------------- fetch

def step_fetch(account: str, since: str, limit: int, local: bool) -> None:
    out_csv = os.path.join(OUT_DIR, f"{account}_posts.csv")
    os.makedirs(OUT_DIR, exist_ok=True)

    if local or _on_vps():
        _run([sys.executable, "scripts/fetch_x_posts.py", "--username", account,
              "--since", since, "--limit", str(limit), "--output", out_csv])
        return

    remote_tmp = f"/tmp/{account}_posts.csv"
    # The X session is shared with the Trent watcher; concurrent UserTweets calls
    # burn the same quota and one of the two will come back empty.
    print(f"Pausing {SHARED_X_TIMER} for the duration of the fetch")
    _ssh(f"systemctl stop {SHARED_X_TIMER}")
    try:
        cmd = (f'su - forwarder -c "cd {VPS_APP} && '
               f'~/venv/bin/python scripts/fetch_x_posts.py '
               f'--username {shlex.quote(account)} --since {shlex.quote(since)} '
               f'--limit {limit} --output {remote_tmp}"')
        print(f"Fetching on {VPS_HOST} (this waits out X rate limits; can take a while)")
        out = _ssh(cmd)
        for line in out.splitlines():
            if "WARNING" not in line and "INFO" not in line and line.strip():
                print("  " + line, flush=True)
    finally:
        _ssh(f"systemctl start {SHARED_X_TIMER}", check=False)
        print(f"Restarted {SHARED_X_TIMER}")

    subprocess.run(["scp", "-q", f"{VPS_HOST}:{remote_tmp}", out_csv], check=True)
    _ssh(f"rm -f {remote_tmp}", check=False)
    print(f"Pulled {out_csv}")


# --------------------------------------------------------------------------- report

def report_exclusions(account: str, keep: set[str]) -> None:
    """Show every parsed pick that will NOT be graded, with its tweet link.

    The category split is the whole reason the graded record means anything, so
    it should be visible by default rather than something you go digging for.
    A "result" post reveals a pick only after it won; counting those manufactures
    a near-perfect record out of nothing.
    """
    path = os.path.join(OUT_DIR, f"{account}_parsed.csv")
    if not os.path.isfile(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    excluded = [r for r in rows if r.get("category", "") not in keep]
    if not excluded:
        print("\nNo excluded picks — every parsed pick is in a graded category.")
        return

    _hr(f"EXCLUDED from grading: {len(excluded)} of {len(rows)} parsed picks")
    print("These are parsed but deliberately not graded. Categories kept: "
          f"{sorted(keep)}\n")
    by_cat: dict[str, list[dict]] = {}
    for r in excluded:
        by_cat.setdefault(r.get("category", "?"), []).append(r)

    md = [f"# {account} — picks excluded from grading\n",
          f"Kept categories: `{sorted(keep)}`\n"]
    for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        print(f"--- {cat} ({len(items)}) ---")
        md.append(f"\n## {cat} ({len(items)})\n")
        md.append("| Date | Sport | Pick | Tweet |")
        md.append("|---|---|---|---|")
        for r in sorted(items, key=lambda x: x["date"]):
            # URLs come straight from the CSV — never reconstructed from an id.
            print(f"  {r['date'][:10]}  {r.get('sport',''):7s} "
                  f"{r.get('description','')[:52]:52s}  {r.get('url','')}")
            desc = r.get("description", "").replace("|", "\\|")
            md.append(f"| {r['date'][:10]} | {r.get('sport','')} | {desc} "
                      f"| {r.get('url','')} |")
        print()
    out_md = os.path.join(OUT_DIR, f"{account}_excluded.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print(f"Written to {out_md}")


def report_summary(account: str) -> None:
    path = os.path.join(OUT_DIR, f"{account}_sheet.csv")
    if not os.path.isfile(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return
    _hr("SUMMARY")
    w = sum(1 for r in rows if r["W/L"] == "win")
    l = sum(1 for r in rows if r["W/L"] == "lose")
    p = sum(1 for r in rows if r["W/L"] == "push")
    tot = sum(float(r["Return"]) for r in rows)
    print(f"{w}W - {l}L - {p}P over {len(rows)} bets")
    if w + l:
        print(f"Win rate: {100 * w / (w + l):.1f}%")
    print(f"Units: {tot:+.2f}U   ROI: {100 * tot / len(rows):+.1f}%")

    by: dict[str, list[float]] = {}
    for r in rows:
        by.setdefault(r["League"], []).append(float(r["Return"]))
    print("\nBy league:")
    for lg, rets in sorted(by.items(), key=lambda kv: -len(kv[1])):
        print(f"  {lg:7s} {len(rets):3d} bets  {sum(rets):+7.2f}U")


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Twitter capper -> graded Google Sheet, in one command.")
    ap.add_argument("--account", required=True, help="X handle without the @")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD (required for fetch)")
    ap.add_argument("--limit", type=int, default=2000, help="Max tweets to scan")
    ap.add_argument("--from", dest="from_step", choices=STEPS, default=None,
                    help="Start at this step, reusing earlier outputs")
    ap.add_argument("--only", choices=STEPS, default=None, help="Run one step")
    ap.add_argument("--local-fetch", action="store_true",
                    help="Fetch locally instead of on the VPS")
    ap.add_argument("--skip-images", action="store_true",
                    help="Text-only parse (cheaper, misses image-only picks)")
    ap.add_argument("--categories", default=None,
                    help='Parse categories to grade, or "all"')
    ap.add_argument("--sheet-id", default=None, help="Target workbook id or URL")
    ap.add_argument("--no-sheet", action="store_true", help="Skip the Sheets upload")
    args = ap.parse_args()

    if args.only:
        steps = [args.only]
    else:
        start = STEPS.index(args.from_step) if args.from_step else 0
        steps = STEPS[start:]
    if args.no_sheet and "export" in steps:
        steps.remove("export")
    if "fetch" in steps and not args.since:
        ap.error("--since is required when running the fetch step")

    acct = args.account
    t0 = time.time()
    print(f"Backfilling @{acct}  |  steps: {' -> '.join(steps)}")

    if "fetch" in steps:
        _hr("1/5 FETCH")
        step_fetch(acct, args.since, args.limit, args.local_fetch)

    if "parse" in steps:
        _hr("2/5 PARSE")
        cmd = [sys.executable, "scripts/parse_posts_csv.py", "--account", acct]
        if args.skip_images:
            cmd.append("--skip-images")
        _run(cmd)

    if "grade" in steps:
        _hr("3/5 GRADE")
        cmd = [sys.executable, "scripts/grade_csv.py", "--account", acct]
        if args.categories:
            cmd += ["--categories", args.categories]
        _run(cmd)

    if "format" in steps:
        _hr("4/5 FORMAT")
        _run([sys.executable, "scripts/format_graded_csv.py", "--account", acct])

    if "export" in steps:
        _hr("5/5 EXPORT TO GOOGLE SHEETS")
        from scripts.sheets_export import export, SheetsExportError
        try:
            export(acct, args.sheet_id)
        except SheetsExportError as e:
            print(f"Sheets upload skipped: {e}")
            print(f"CSV is ready at {OUT_DIR}\\{acct}_sheet.csv")

    from scripts.parse_posts_csv import GRADEABLE_CATEGORIES
    keep = ({c.strip() for c in args.categories.split(",")}
            if args.categories else set(GRADEABLE_CATEGORIES))
    report_exclusions(acct, keep)
    report_summary(acct)
    print(f"\nDone in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        print(f"\nStep failed (exit {e.returncode}). Fix it, then resume with --from.")
        sys.exit(e.returncode)
