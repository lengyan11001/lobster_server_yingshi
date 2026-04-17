import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('42.194.209.150', username='ubuntu', password='|EP^q4r5-)f2k', timeout=15)

cmds = [
    # Search for backend.mcp in ALL logs
    'grep -r "backend.mcp" /opt/lobster-server/logs/ 2>/dev/null | wc -l',

    # Search in full app.log for error prefix
    'grep -c "\\\\u9519\\\\u8bef" /opt/lobster-server/logs/app.log 2>/dev/null || echo 0',

    # Search for the Chinese "错误" in the logs (using hex)
    'python3 -c "data=open(\'/opt/lobster-server/logs/app.log\',\'rb\').read(); print(f\'backend.mcp count: {data.count(b\"backend.mcp\")}\'); print(f\'run_chat error count: {data.count(b\"run_chat error\")}\'); print(f\'chat/stream count: {data.count(b\"/chat/stream\")}\'); print(f\'chat error count: {data.count(b\"error_holder\")}\')" 2>&1',

    # Check if there's a /chat/stream request that returned error
    'grep "chat/stream" /opt/lobster-server/logs/app.log 2>/dev/null | tail -20',

    # Check the chat request flow 
    'grep -E "(\\[CHAT|\\[chat|chat_stream|run_chat)" /opt/lobster-server/logs/app.log 2>/dev/null | tail -20',

    # Check /chat/stream HTTP requests in the access log
    'grep "chat/stream" /opt/lobster-server/logs/app.log 2>/dev/null | grep -v "WeCom" | tail -20',

    # Check all POST requests from the journal
    'journalctl -u lobster-backend --no-pager --since "30 minutes ago" 2>/dev/null | grep -E "POST.*chat" | tail -20',

    # Check for any error-related output from the backend
    'journalctl -u lobster-backend --no-pager --since "30 minutes ago" 2>/dev/null | grep -iE "(error|exception|traceback|module)" | tail -20',
]

for cmd in cmds:
    print(f'=== {cmd} ===')
    _, o, e = c.exec_command(cmd, timeout=15)
    print(o.read().decode(errors='replace'))
    if (err := e.read().decode(errors='replace')):
        print(f'STDERR: {err}')
    print()

c.close()
