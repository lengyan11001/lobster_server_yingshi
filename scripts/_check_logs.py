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
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if out:
        print(out)
    if err and err != out:
        print("ERR:", err[:500])

print("=== Last 50 lines with direct/deepseek/timeout/504/attempt ===")
run(f"grep -iE 'direct|deepseek|timeout|504|attempt|trace_id' {DIR}/logs/backend.log 2>/dev/null | tail -50")

print("\n=== Last 30 lines of backend.log ===")
run(f"tail -30 {DIR}/logs/backend.log 2>/dev/null")

print("\n=== Last 20 lines of app.log ===")
run(f"tail -20 {DIR}/logs/app.log 2>/dev/null")

print("\n=== Service status ===")
run("systemctl status lobster-backend --no-pager -l 2>/dev/null | tail -10")

ssh.close()
