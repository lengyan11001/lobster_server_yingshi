import paramiko
import urllib.request
import json

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('42.194.209.150', username='ubuntu', password='|EP^q4r5-)f2k', timeout=15)

checks = [
    ('Services status', 'systemctl is-active lobster-backend; systemctl is-active lobster-mcp'),
    ('Current commit', 'cd /opt/lobster-server && git log --oneline -1'),
    ('Backend log (last 10 lines)', 'tail -10 /opt/lobster-server/backend.log 2>&1'),
    ('MCP log (last 5 lines)', 'tail -5 /opt/lobster-server/mcp.log 2>&1'),
    ('Port check', 'ss -tlnp | grep -E "(8000|8001)" 2>&1'),
]

for desc, cmd in checks:
    print(f'[{desc}]')
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=30)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if out:
        print(out)
    if err:
        print('STDERR:', err)
    print()

ssh.close()

print('[HTTP health check from local]')
try:
    req = urllib.request.Request('http://42.194.209.150:8000/api/health', method='GET')
    req.add_header('User-Agent', 'deploy-check')
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(f'Status: {resp.status}')
        print(f'Body: {resp.read().decode()[:200]}')
except Exception as e:
    print(f'Health check failed: {e}')

print('\nVerification complete.')
