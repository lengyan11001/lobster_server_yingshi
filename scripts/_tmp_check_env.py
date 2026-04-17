#!/usr/bin/env python3
import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)

cmd = """cd /opt/lobster-server && cat .env | grep -i database; echo '---'; ls -la *.db data/*.db 2>/dev/null; echo '---'; journalctl -u lobster-backend --no-pager -n 30 2>&1 | grep -i -E 'pending|wecom|database|sqlite|error' | head -20"""

stdin, stdout, stderr = client.exec_command(cmd)
print(stdout.read().decode().strip())
err = stderr.read().decode().strip()
if err:
    print("STDERR:", err)
client.close()
