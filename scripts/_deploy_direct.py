"""Deploy direct API routing changes + configure DEEPSEEK_API_KEY."""
import paramiko

HOST = "42.194.209.150"
USER = "ubuntu"
PASS = "|EP^q4r5-)f2k"
DS_KEY = "sk-1f6b07ab8b4a4dccb9444e774d124e67"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

def run(cmd, timeout=60):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    print(f"  OUT: {out[:500]}" if out else "  OUT: (empty)")
    if err:
        print(f"  ERR: {err[:300]}")
    return out

print("=== 1. Git pull latest ===")
run("cd /opt/lobster-server && git fetch origin && git reset --hard origin/main")

print("\n=== 2. Check commit ===")
run("cd /opt/lobster-server && git log -1 --oneline")

print("\n=== 3. Add DEEPSEEK_API_KEY to .env ===")
# Check if already present
out = run("grep -c DEEPSEEK_API_KEY /opt/lobster-server/.env || echo 0")
if out.strip() == "0" or out.strip() == "":
    run(f"echo 'DEEPSEEK_API_KEY={DS_KEY}' >> /opt/lobster-server/.env")
    print("  Added DEEPSEEK_API_KEY to .env")
else:
    # Update existing
    run(f"sed -i 's|^DEEPSEEK_API_KEY=.*|DEEPSEEK_API_KEY={DS_KEY}|' /opt/lobster-server/.env")
    print("  Updated existing DEEPSEEK_API_KEY in .env")

print("\n=== 4. Verify .env has key ===")
run("grep DEEPSEEK_API_KEY /opt/lobster-server/.env | head -1 | cut -c1-30")

print("\n=== 5. Restart services ===")
run("cd /opt/lobster-server && bash scripts/server_update_and_restart.sh", timeout=90)

print("\n=== 6. Wait and check logs ===")
import time
time.sleep(5)
run("tail -30 /opt/lobster-server/logs/app.log | grep -i 'direct\\|deepseek\\|attempt\\|route' | tail -10")

print("\n=== 7. Check service status ===")
run("supervisorctl status 2>/dev/null || systemctl status lobster-backend --no-pager -l 2>/dev/null | tail -5")

ssh.close()
print("\nDone!")
