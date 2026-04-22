#!/usr/bin/env python3
"""One-off: set brand_mark=yingshi for known phone-account emails. Run on server: python3 scripts/_tmp_fix_brand_mark_users.py"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "lobster.db"

PHONES = ("15061138170", "16649862287", "13729965525", "19924552278")
SUFFIX = "@sms.lobster.local"
EMAILS = [f"{p}{SUFFIX}" for p in PHONES]


def main() -> int:
    if not DB.is_file():
        print(f"[ERR] missing db: {DB}")
        return 1
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()
    print("[before]")
    for em in EMAILS:
        row = cur.execute(
            "SELECT id, email, brand_mark FROM users WHERE email = ?", (em,)
        ).fetchone()
        print(row or f"  (no row) {em}")
    cur.executemany(
        "UPDATE users SET brand_mark = ? WHERE email = ?",
        [("yingshi", em) for em in EMAILS],
    )
    conn.commit()
    print(f"[updated rows] {cur.rowcount}")
    print("[after]")
    for em in EMAILS:
        row = cur.execute(
            "SELECT id, email, brand_mark FROM users WHERE email = ?", (em,)
        ).fetchone()
        print(row or f"  (no row) {em}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
