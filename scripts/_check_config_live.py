import paramiko, time
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)

def run(cmd, timeout=30):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return out, err

# Check what .env contains
print("=== .env DEEPSEEK lines ===")
out, _ = run("grep -n DEEPSEEK /opt/lobster-server/.env")
print(out if out else "NOT FOUND")

# Check config loads correctly
print("\n=== Python config test ===")
out, err = run("cd /opt/lobster-server && .venv/bin/python3 -c 'from backend.app.core.config import settings; print(\"deepseek_api_key:\", repr(settings.deepseek_api_key)); print(\"deepseek_api_base:\", repr(settings.deepseek_api_base))'")
print(out)
if err:
    print("ERR:", err[:300])

# Check direct route function
print("\n=== Direct route test ===")
out, err = run("cd /opt/lobster-server && .venv/bin/python3 -c 'from backend.app.api.sutui_chat_proxy import _get_direct_route; print(\"deepseek-chat:\", _get_direct_route(\"deepseek-chat\")); print(\"claude-opus-4-6:\", _get_direct_route(\"claude-opus-4-6\"))'")
print(out)
if err:
    print("ERR:", err[:300])

# Check when service was last restarted
print("\n=== Service start time ===")
out, _ = run("systemctl show lobster-backend --property=ActiveEnterTimestamp 2>/dev/null || echo unknown")
print(out)

# Check if any new requests came since restart
print("\n=== Recent attempts logs (since 18:33) ===")
out, _ = run("grep 'attempts=' /opt/lobster-server/logs/app.log | tail -5")
print(out if out else "(no attempts logs)")

ssh.close()
