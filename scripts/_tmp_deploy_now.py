import paramiko
import time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('42.194.209.150', username='ubuntu', password='|EP^q4r5-)f2k', timeout=15)

commands = [
    ('Recording rollback commit', 'cd /opt/lobster-server && git rev-parse HEAD > .deploy_rollback_commit'),
    ('Git fetch', 'cd /opt/lobster-server && git fetch origin main'),
    ('Git pull', 'cd /opt/lobster-server && git pull origin main'),
    ('Show current commit', 'cd /opt/lobster-server && git log --oneline -3'),
    ('Install dependencies', 'cd /opt/lobster-server && .venv/bin/pip install -r requirements.txt -q 2>&1 | tail -5'),
    ('Restart lobster-backend', 'sudo systemctl restart lobster-backend'),
    ('Restart lobster-mcp', 'sudo systemctl restart lobster-mcp'),
    ('Wait for services', 'sleep 3'),
    ('Check lobster-backend status', 'systemctl is-active lobster-backend'),
    ('Check lobster-mcp status', 'systemctl is-active lobster-mcp'),
    ('Check processes', 'ps aux | grep -E "(uvicorn|backend.run|mcp)" | grep -v grep'),
]

for desc, cmd in commands:
    print(f'[{desc}]')
    print(f'  $ {cmd}')
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=120)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if out:
        print(f'  {out}')
    if err:
        print(f'  STDERR: {err}')
    print()

ssh.close()
print('Deploy complete.')
