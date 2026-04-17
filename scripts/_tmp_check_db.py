#!/usr/bin/env python3
import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)

cmd = """cd /opt/lobster-server && python3 << 'PYEOF'
import os

# Check .env for DATABASE_URL
env_path = ".env"
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if "DATABASE" in line.upper() or "DB" in line.upper() or "SQLITE" in line.upper():
                print("ENV:", line)

# Check data/ directory
print("\\nFiles in data/:", os.listdir("data") if os.path.isdir("data") else "NO data/ dir")
print("Files in root:", [f for f in os.listdir(".") if f.endswith(".db")])

# Check both databases
import sqlite3
for db_path in ["lobster.db", "data/lobster.db"]:
    if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM wecom_pending_messages WHERE status='pending'").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM wecom_pending_messages").fetchone()[0]
        print(f"\\n{db_path}: pending={count}, total={total}")
        conn.close()
    else:
        print(f"\\n{db_path}: empty or not exists")
PYEOF"""

stdin, stdout, stderr = client.exec_command(cmd)
print(stdout.read().decode().strip())
err = stderr.read().decode().strip()
if err:
    print("STDERR:", err)
client.close()
