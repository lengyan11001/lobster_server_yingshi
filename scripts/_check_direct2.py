import paramiko

HOST = "42.194.209.150"
USER = "ubuntu"
PASS = "|EP^q4r5-)f2k"
DIR = "/opt/lobster-server"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

def run(cmd):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=30)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    return out

print("=== Check attempts log in app.log ===")
out = run(f"grep 'attempts=' {DIR}/logs/app.log | tail -5")
print(out if out else "(no attempts log found)")

print("\n=== Check _get_direct_route function in deployed code ===")
out = run(f"grep -A10 'def _get_direct_route' {DIR}/backend/app/api/sutui_chat_proxy.py")
print(out)

print("\n=== Live test: call _get_direct_route ===")
out = run(f'''cd {DIR} && PYTHONPATH=. .venv/bin/python3 -c "
from backend.app.api.sutui_chat_proxy import _get_direct_route
result = _get_direct_route('deepseek-chat')
print('_get_direct_route result:', result)
"''')
print(out if out else "(failed)")

print("\n=== Check if settings object has deepseek_api_key at import time ===")
out = run(f'''cd {DIR} && PYTHONPATH=. .venv/bin/python3 -c "
from backend.app.core.config import settings
print('settings.deepseek_api_key:', repr(settings.deepseek_api_key[:15] + '...' if settings.deepseek_api_key else 'NONE'))
"''')
print(out if out else "(failed)")

ssh.close()
