#!/usr/bin/env bash
# 由本机执行：bash scripts/ssh_run_remote.sh "bash -s" < scripts/remote_inspect_abc123_ledger.sh
# 远端：查用户 abc123 与近期 credit_ledger
set -euo pipefail
cd /root/lobster_server
python3 <<'PY'
import sqlite3
c = sqlite3.connect("lobster.db")
# 站内账号为 email，无 username 列
r = c.execute(
    "SELECT id,email,brand_mark,credits FROM users WHERE email LIKE ?",
    ("%abc123%",),
).fetchone()
if not r:
    r = c.execute(
        "SELECT id,email,brand_mark,credits FROM users WHERE email = ?",
        ("abc123",),
    ).fetchone()
print("user_row", r)
if not r:
    print("no user matching abc123 in email")
    raise SystemExit(0)
uid = r[0]
q = """SELECT id,entry_type,description,ref_type,delta,created_at
FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT 40"""
for row in c.execute(q, (uid,)):
    print(row)
print("--- capability_call_logs last 15 ---")
for row in c.execute(
    "SELECT id,capability_id,credits_charged,success,created_at FROM capability_call_logs WHERE user_id=? ORDER BY id DESC LIMIT 15",
    (uid,),
):
    print(row)
PY
