#!/usr/bin/env python3
"""Check systemd service files and how MCP is started."""
import paramiko

HOST = "42.194.209.150"
USER = "ubuntu"
PASSWORD = "|EP^q4r5-)f2k"

cmds = [
    "cat /etc/systemd/system/lobster-mcp.service 2>/dev/null",
    "cat /etc/systemd/system/lobster-backend.service 2>/dev/null",
    "systemctl is-active lobster-mcp.service",
    "systemctl is-active lobster-backend.service",
    # Check what command was used to start MCP
    "ps aux | grep mcp | grep -v grep",
    # Check last MCP service logs
    "journalctl -u lobster-mcp -n 20 --no-pager 2>/dev/null",
    # Check if backend started MCP subprocess
    "journalctl -u lobster-backend --since '10 min ago' --no-pager 2>/dev/null | grep -i 'mcp\\|backend.mcp\\|module\\|import' | head -20",
    # Check lobster-server run.py to see if it starts MCP
    "grep -n 'mcp\\|subprocess\\|Popen\\|backend.mcp' /opt/lobster-server/backend/run.py 2>/dev/null",
]

def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=15)
    for cmd in cmds:
        print(f"\n=== {cmd[:80]} ===")
        stdin, stdout, stderr = client.exec_command(cmd)
        out = stdout.read().decode()
        err = stderr.read().decode().strip()
        print(out[:2000] if out else "(empty)")
        if err:
            print(f"STDERR: {err[:500]}")
    client.close()

if __name__ == "__main__":
    main()
