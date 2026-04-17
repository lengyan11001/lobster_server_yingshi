import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('42.194.209.150', username='ubuntu', password='|EP^q4r5-)f2k', timeout=15)

cmds = [
    # Get the very latest app.log entries
    'tail -30 /opt/lobster-server/logs/app.log 2>/dev/null | grep -v "WeCom.*GET pending"',

    # Check for any MCP invoke after 22:03
    'journalctl -u lobster-mcp --no-pager --since "22:03" 2>/dev/null | tail -20',

    # Check if there's a video.generate execution log
    'journalctl -u lobster-backend --no-pager --since "22:03" 2>/dev/null | grep -v "WeCom.*GET pending" | grep -v "DEBUG.*http" | tail -30',

    # Count of backend.mcp in NEW process only (PID 1819490)
    'journalctl -u lobster-backend --no-pager --since "21:56" 2>/dev/null | grep -c "backend.mcp" || echo 0',

    # Count in OLD process (before restart)  
    'journalctl -u lobster-backend --no-pager --until "21:56" --since "21:50" 2>/dev/null | grep -c "backend.mcp" || echo 0',
]

for cmd in cmds:
    print(f'=== {cmd} ===')
    _, o, e = c.exec_command(cmd, timeout=15)
    print(o.read().decode(errors='replace'))
    if (err := e.read().decode(errors='replace')):
        print(f'STDERR: {err}')
    print()

c.close()
