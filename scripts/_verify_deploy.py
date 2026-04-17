import paramiko, time
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)

def run(cmd, timeout=30):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return out, err

print("=== Restart services ===")
out, err = run("cd /opt/lobster-server && bash scripts/server_update_and_restart.sh", timeout=90)
print(out[-300:] if out else "(empty)")

print("\n=== Wait 8s for startup ===")
time.sleep(8)

print("\n=== Check config loaded ===")
out, err = run("cd /opt/lobster-server && .venv/bin/python3 -c 'from backend.app.core.config import settings; k=settings.deepseek_api_key or \"\"; print(\"key_loaded:\", bool(k), k[:12] if k else \"NONE\")'")
print(out)
if err:
    print("ERR:", err[:300])

print("\n=== Check recent logs for direct/attempt ===")
out, _ = run("tail -100 /opt/lobster-server/logs/app.log | grep -iE 'attempts=|direct|route=' | tail -15")
print(out if out else "(no matching log entries)")

print("\n=== Check service running ===")
out, _ = run("pgrep -a -f 'python.*backend' | head -3")
print(out if out else "(no processes)")

ssh.close()
print("\nDone")
