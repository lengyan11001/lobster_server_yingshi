import paramiko, time

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('42.194.209.150', username='ubuntu', password='|EP^q4r5-)f2k', timeout=15)

cmds = [
    # Get the VERY latest logs - filter out WeCom polling noise
    'journalctl -u lobster-backend --no-pager --since "2 minutes ago" 2>/dev/null | grep -v "WeCom.*GET pending" | grep -v "DEBUG.*http"',

    # Specifically look for error/traceback/module errors  
    'journalctl -u lobster-backend --no-pager --since "5 minutes ago" 2>/dev/null | grep -iE "(error|traceback|module|chat/stream|run_chat|backend.mcp)"',

    # Check MCP logs for the same period
    'journalctl -u lobster-mcp --no-pager --since "5 minutes ago" 2>/dev/null',

    # Check app.log for VERY latest non-WeCom entries
    'grep -v "WeCom.*GET pending" /opt/lobster-server/logs/app.log | tail -40',
]

for cmd in cmds:
    print(f'=== {cmd[:80]}... ===')
    _, o, e = c.exec_command(cmd, timeout=15)
    out = o.read().decode(errors='replace')
    print(out[:8000] if out else "(empty)")
    print()

c.close()
