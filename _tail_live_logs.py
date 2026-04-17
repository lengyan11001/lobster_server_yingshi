"""Tail the live logs on the server to catch the error in real time."""
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('42.194.209.150', username='ubuntu', password='|EP^q4r5-)f2k', timeout=15)

cmds = [
    # Check latest app.log entries (last 200 lines)
    'tail -200 /opt/lobster-server/logs/app.log 2>/dev/null | grep -v "WeCom.*GET pending"',

    # Check journalctl for lobster-backend errors
    'journalctl -u lobster-backend --no-pager -n 100 2>/dev/null | grep -v "WeCom.*GET pending" | tail -60',

    # Check for ANY recent error or traceback
    'journalctl -u lobster-backend --no-pager --since "5 minutes ago" 2>/dev/null | grep -v "WeCom.*GET pending"',

    # Check journalctl for lobster-mcp
    'journalctl -u lobster-mcp --no-pager -n 30 2>/dev/null',

    # Check if there are any error logs we're missing
    'find /opt/lobster-server -name "*.log" -newer /opt/lobster-server/logs/app.log 2>/dev/null',
    'ls -la /opt/lobster-server/logs/',

    # Check stderr of the backend process
    'journalctl -u lobster-backend --no-pager -n 50 -p err 2>/dev/null || echo "no error priority logs"',
]

for cmd in cmds:
    print(f'=== {cmd} ===')
    _, o, e = c.exec_command(cmd, timeout=15)
    out = o.read().decode(errors='replace')
    err_out = e.read().decode(errors='replace')
    print(out[:5000] if out else "(empty)")
    if err_out:
        print(f'STDERR: {err_out[:500]}')
    print()

c.close()
