#!/usr/bin/env python3
"""Check MCP logs for video.generate calls and errors."""
import paramiko

HOST = "42.194.209.150"
USER = "ubuntu"
PASSWORD = "|EP^q4r5-)f2k"

cmds = [
    # MCP log for video.generate 
    "grep -i 'video.generate\\|backend.mcp\\|ModuleNotFoundError\\|error\\|traceback' /opt/lobster-server/mcp.log 2>/dev/null | tail -30",
    "grep -i 'video.generate\\|backend.mcp\\|ModuleNotFoundError\\|error\\|traceback' /opt/lobster-server/logs/mcp.log 2>/dev/null | tail -30",
    
    # Check backend app.log for /chat/stream entries
    "grep -c '/chat/stream' /opt/lobster-server/logs/app.log 2>/dev/null || echo 0",
    "grep '/chat/stream' /opt/lobster-server/logs/app.log 2>/dev/null | tail -10",
    
    # Check for the chat error pattern
    "grep -i 'chat/stream run_chat error\\|错误：.*backend.mcp\\|No module named' /opt/lobster-server/logs/app.log 2>/dev/null | tail -20",
    
    # Check the FULL journal for last 10 minutes including ALL entries
    "journalctl -u lobster-backend --since '10 min ago' --no-pager 2>/dev/null | grep -i 'video.generate\\|MCP.*请求\\|工具.*异常\\|run_chat error\\|backend.mcp\\|No module' | tail -30",
    
    # Check if the /chat/stream endpoint is even registered (imported)
    "grep -n 'chat/stream\\|chat_stream' /opt/lobster-server/backend/app/api/chat.py 2>/dev/null | head -10",
    
    # Check the router includes
    "grep -rn 'include_router\\|chat\\|APIRouter' /opt/lobster-server/backend/app/create_app.py 2>/dev/null | head -20",
    
    # Check online edition setting
    "grep -i 'LOBSTER_EDITION\\|edition' /opt/lobster-server/.env 2>/dev/null",
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
        print(out[:3000] if out else "(empty)")
        if err:
            print(f"STDERR: {err[:500]}")
    client.close()

if __name__ == "__main__":
    main()
