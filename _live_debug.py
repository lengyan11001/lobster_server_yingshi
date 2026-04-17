#!/usr/bin/env python3
"""Real-time check: tail recent logs and check for backend.mcp error."""
import paramiko
import time

HOST = "42.194.209.150"
USER = "ubuntu"
PASSWORD = "|EP^q4r5-)f2k"

def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=15)
    print("Connected\n")

    # 1. Check current PID and uptime
    stdin, stdout, stderr = client.exec_command("systemctl show lobster-backend --property=MainPID,ActiveEnterTimestamp")
    print("=== Service Info ===")
    print(stdout.read().decode())

    # 2. Check the filter is actually in the deployed file
    stdin, stdout, stderr = client.exec_command(
        "python3 -c \""
        "with open('/opt/lobster-server/backend/app/api/sutui_chat_proxy.py','rb') as f: data=f.read();"
        "needle='错误：'.encode('utf-8');"
        "idx=data.find(needle);"
        "print(f'Filter found at byte {idx}');"
        "if idx>=0: print(repr(data[max(0,idx-80):idx+200]));"
        "\""
    )
    print("=== Filter Check ===")
    print(stdout.read().decode())
    print(stderr.read().decode())

    # 3. Get the LAST 200 lines of journalctl (recent activity)
    stdin, stdout, stderr = client.exec_command(
        "journalctl -u lobster-backend -n 200 --no-pager 2>/dev/null"
    )
    logs = stdout.read().decode()
    lines = logs.strip().split("\n")
    print(f"=== Last 200 journal lines ({len(lines)} actual) ===")

    # Filter for interesting lines
    for line in lines:
        lower = line.lower()
        if any(kw in lower for kw in ["backend.mcp", "error", "错误", "exception", "traceback", "module", "chat/stream", "sutui-chat", "video"]):
            print(line)

    # 4. Check app.log
    print("\n=== app.log last 50 lines (filtered) ===")
    stdin, stdout, stderr = client.exec_command(
        "tail -50 /opt/lobster-server/logs/app.log 2>/dev/null"
    )
    app_log = stdout.read().decode()
    for line in app_log.strip().split("\n"):
        lower = line.lower()
        if any(kw in lower for kw in ["backend.mcp", "error", "错误", "module", "video", "sora"]):
            print(line)

    # 5. Check mcp.log
    print("\n=== mcp.log last 50 lines (filtered) ===")
    stdin, stdout, stderr = client.exec_command(
        "tail -50 /opt/lobster-server/logs/mcp.log 2>/dev/null"
    )
    mcp_log = stdout.read().decode()
    for line in mcp_log.strip().split("\n"):
        lower = line.lower()
        if any(kw in lower for kw in ["backend.mcp", "error", "错误", "module", "video", "sora"]):
            print(line)

    # 6. Check if there's a frontend server (lobster_online) running
    print("\n=== Process check: any lobster_online or other python servers ===")
    stdin, stdout, stderr = client.exec_command(
        "ps aux | grep -E 'lobster|python|uvicorn|gunicorn' | grep -v grep"
    )
    print(stdout.read().decode())

    # 7. Check what the proxy does with the last request - look at full recent journal
    print("\n=== Full recent journal entries with sutui-chat ===")
    stdin, stdout, stderr = client.exec_command(
        "journalctl -u lobster-backend --since '5 min ago' --no-pager 2>/dev/null | grep -i 'sutui\\|chat\\|backend.mcp\\|error\\|video' | head -50"
    )
    print(stdout.read().decode())

    # 8. Check if the ACTUAL running code has the filter
    print("\n=== Verify running code has filter (check /proc) ===")
    stdin, stdout, stderr = client.exec_command(
        "systemctl show lobster-backend --property=MainPID"
    )
    pid_line = stdout.read().decode().strip()
    pid = pid_line.split("=")[1] if "=" in pid_line else ""
    print(f"Main PID: {pid}")

    if pid and pid != "0":
        stdin, stdout, stderr = client.exec_command(
            f"ls -la /proc/{pid}/exe 2>/dev/null; cat /proc/{pid}/cmdline 2>/dev/null | tr '\\0' ' '"
        )
        print(stdout.read().decode())

    client.close()
    print("\nDone")

if __name__ == "__main__":
    main()
