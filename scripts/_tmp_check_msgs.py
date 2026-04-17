#!/usr/bin/env python3
import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)

cmd = """cd /opt/lobster-server && python3 << 'PYEOF'
import sqlite3, json

conn = sqlite3.connect("lobster.db")
conn.row_factory = sqlite3.Row

cols = [c[1] for c in conn.execute("PRAGMA table_info(wecom_messages)").fetchall()]
print("columns:", cols)
rows = conn.execute("SELECT * FROM wecom_messages ORDER BY id DESC LIMIT 20").fetchall()
print("=== Recent messages ===")
for r in rows:
    print(json.dumps(dict(r), ensure_ascii=False, default=str))

prows = conn.execute("SELECT * FROM wecom_pending_messages ORDER BY id DESC LIMIT 10").fetchall()
print("\\n=== Pending messages ===")
for r in prows:
    print(json.dumps(dict(r), ensure_ascii=False, default=str))

conn.close()
PYEOF"""

stdin, stdout, stderr = client.exec_command(cmd)
print(stdout.read().decode().strip())
err = stderr.read().decode().strip()
if err:
    print("STDERR:", err)
client.close()
