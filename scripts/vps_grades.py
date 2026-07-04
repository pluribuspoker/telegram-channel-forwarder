"""Query the grades DB on VPS.

Usage (on VPS as forwarder):
    ~/venv/bin/python scripts/vps_grades.py                        # last 20
    ~/venv/bin/python scripts/vps_grades.py --msg-id 3243          # by message ID
    ~/venv/bin/python scripts/vps_grades.py --search "Argentina"   # by text in pick_desc
"""
import argparse
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--msg-id", type=int, help="Filter by message_id")
    parser.add_argument("--search", help="Search pick_desc for substring")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    conn = sqlite3.connect("picks.db")
    conn.row_factory = sqlite3.Row

    if args.msg_id:
        rows = conn.execute(
            "SELECT * FROM grades WHERE message_id = ?", (args.msg_id,)
        ).fetchall()
    elif args.search:
        rows = conn.execute(
            "SELECT * FROM grades WHERE pick_desc LIKE ? ORDER BY graded_at DESC LIMIT ?",
            (f"%{args.search}%", args.limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM grades ORDER BY graded_at DESC LIMIT ?", (args.limit,)
        ).fetchall()

    if not rows:
        print("No rows found")
        return

    for r in rows:
        for k in r.keys():
            print(f"  {k}: {r[k]}")
        print("---")

    print(f"({len(rows)} row(s))")


if __name__ == "__main__":
    main()
