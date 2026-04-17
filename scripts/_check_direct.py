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

print("=== Check DEEPSEEK_API_KEY in .env ===")
out = run(f"grep DEEPSEEK_API_KEY {DIR}/.env")
print(out if out else "(NOT FOUND)")

print("\n=== Check config.py has deepseek_api_key ===")
out = run(f"grep -n deepseek_api {DIR}/backend/app/core/config.py | head -5")
print(out if out else "(NOT FOUND in config.py)")

print("\n=== Check sutui_chat_proxy imports ===")
out = run(f"grep -n 'direct\\|_get_direct_route\\|credits_from_direct' {DIR}/backend/app/api/sutui_chat_proxy.py | head -10")
print(out if out else "(NOT FOUND)")

print("\n=== Check attempts list building ===")
out = run(f"grep -n 'attempts\\|_DIRECT_TIMEOUT\\|_XSKILL_TIMEOUT\\|direct:' {DIR}/backend/app/api/sutui_chat_proxy.py | head -15")
print(out if out else "(NOT FOUND)")

print("\n=== Check current commit ===")
out = run(f"cd {DIR} && git log -1 --oneline")
print(out)

print("\n=== Test direct DeepSeek from server ===")
out = run(f'''cd {DIR} && .venv/bin/python3 -c "
from backend.app.core.config import get_settings
s = get_settings()
print('deepseek_api_key:', repr((s.deepseek_api_key or '')[:10] + '...' if s.deepseek_api_key else 'NONE'))
print('deepseek_api_base:', repr(s.deepseek_api_base))
"''')
print(out if out else "(failed)")

ssh.close()
