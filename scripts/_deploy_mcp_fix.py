import paramiko, sys

HOST = "42.194.209.150"
USER = "ubuntu"
PASS = "|EP^q4r5-)f2k"
REMOTE_DIR = "/opt/lobster-server"

def run(ssh, cmd):
    print(f">>> {cmd}")
    _, stdout, stderr = ssh.exec_command(cmd, timeout=60)
    out = stdout.read().decode()
    err = stderr.read().decode()
    if out.strip():
        print(out.strip())
    if err.strip():
        print("STDERR:", err.strip())
    return out

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    ssh.connect(HOST, username=USER, password=PASS, timeout=15)
except Exception as e:
    print(f"SSH FAILED: {e}")
    sys.exit(1)

run(ssh, f"cd {REMOTE_DIR} && git fetch origin && git reset --hard origin/main 2>&1")
run(ssh, f"cd {REMOTE_DIR} && bash scripts/server_update_and_restart.sh 2>&1")
run(ssh, f"cd {REMOTE_DIR} && git log -1 --oneline")

ssh.close()
print("Done")
