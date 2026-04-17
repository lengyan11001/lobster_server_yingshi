#!/usr/bin/env python3
"""Check how the frontend is deployed and what LOCAL_API_BASE is."""
import paramiko

HOST = "42.194.209.150"
USER = "ubuntu"
PASSWORD = "|EP^q4r5-)f2k"

cmds = [
    "cat /etc/nginx/sites-enabled/lobster 2>/dev/null || echo 'no nginx config'",
    "ls -la /opt/lobster-server/static/ 2>/dev/null | head -20",
    "ls -la /opt/lobster-server/frontend/ 2>/dev/null | head -10",
    "ls -la /opt/lobster_online/static/ 2>/dev/null | head -10",
    "grep -r 'LOCAL_API_BASE\\|LOCAL_LOOPBACK\\|chatBase\\|/chat/stream\\|sutui-chat' /opt/lobster-server/static/js/ 2>/dev/null | head -30",
    "grep -r 'LOCAL_API_BASE' /etc/nginx/ 2>/dev/null | head -10",
    "grep -r 'LOBSTER_LOCAL_LOOPBACK\\|LOCAL_API_BASE' /opt/lobster-server/backend/ --include='*.py' 2>/dev/null | head -20",
    "cat /opt/lobster-server/backend/app/api/chat.py | grep -n 'chat/stream\\|edition\\|online\\|sutui' | head -30",
    "journalctl -u lobster-backend --since '3 min ago' --no-pager 2>/dev/null | grep -i 'chat/stream' | head -10",
]

def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=15)
    for cmd in cmds:
        print(f"\n=== {cmd[:80]} ===")
        stdin, stdout, stderr = client.exec_command(cmd)
        print(stdout.read().decode()[:3000])
        err = stderr.read().decode().strip()
        if err:
            print(f"STDERR: {err[:500]}")
    client.close()

if __name__ == "__main__":
    main()
