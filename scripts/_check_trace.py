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
    return stdout.read().decode("utf-8", errors="replace").strip()

# Last trace ID
tid = "e01f8dafb4764f018c7c359eb9f144b5"
print(f"=== Full trace for {tid} ===")
out = run(f"grep '{tid}' {DIR}/logs/app.log")
print(out if out else "(no entries)")

print(f"\n=== All direct:deepseek entries ===")
out = run(f"grep -i 'direct.*deepseek\\|provider=direct' {DIR}/logs/app.log | tail -10")
print(out if out else "(no entries)")

print(f"\n=== All timeout/error entries in last 20 min ===")
out = run(f"grep -iE 'timeout|ConnectError|520|504|failed' {DIR}/logs/app.log | tail -20")
print(out if out else "(no entries)")

ssh.close()
