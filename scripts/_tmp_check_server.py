import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('42.194.209.150', username='ubuntu', password='|EP^q4r5-)f2k', timeout=15)

cmds = [
    'ls -la /opt/lobster-server/ 2>&1 | head -20',
    'cd /opt/lobster-server && git log --oneline -3 2>&1',
    'systemctl is-active lobster-backend 2>&1',
    'systemctl is-active lobster-mcp 2>&1',
    'ps aux | grep -E "(uvicorn|backend|mcp)" | grep -v grep 2>&1',
    'head -5 /opt/lobster-server/.env 2>&1',
    'python3 --version 2>&1',
    'ls /opt/lobster-server/.venv/bin/python 2>&1',
]

for cmd in cmds:
    print(f'>>> {cmd}')
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode()
    err = stderr.read().decode()
    if out.strip():
        print(out.strip())
    if err.strip():
        print('STDERR:', err.strip())
    print()

ssh.close()
print('Done.')
