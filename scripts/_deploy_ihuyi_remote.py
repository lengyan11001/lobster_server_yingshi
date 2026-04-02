"""按 .env.deploy：SSH 大陆+海外机，git pull + server_update，上传 auth/config/sms 文件，写入 IHUYI_*，再重启。"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_deploy() -> dict[str, str]:
    d: dict[str, str] = {}
    for line in (ROOT / ".env.deploy").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip()
    return d


def norm_key(path: str) -> str:
    path = path.strip()
    if path.startswith("/d/") or path.startswith("/D/"):
        path = "D:" + path[2:].replace("/", os.sep)
    else:
        path = path.replace("/", os.sep)
    return path


def ssh_client_for(host_line: str, pkey):
    import paramiko

    user, _, hostname = host_line.partition("@")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=hostname, username=user, pkey=pkey, timeout=60)
    return c


def run_bash(c, script: str, timeout: int = 300) -> tuple[str, str, int]:
    stdin, stdout, stderr = c.exec_command("bash -s", timeout=timeout)
    stdin.write(script)
    stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return out, err, stdout.channel.recv_exit_status()


def main() -> int:
    import paramiko

    env = load_deploy()
    keyp = norm_key(env["LOBSTER_DEPLOY_SSH_KEY"])
    pp = env.get("LOBSTER_SSH_KEY_PASSPHRASE", "").encode()
    pkey = None
    for Key in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            pkey = Key.from_private_key_file(keyp, password=pp or None)
            break
        except Exception:
            continue
    if not pkey:
        print("cannot load key:", keyp, file=sys.stderr)
        return 1

    targets = [
        (env["LOBSTER_DEPLOY_HOST"], env.get("LOBSTER_DEPLOY_REMOTE_DIR", "/root/lobster_server").rstrip("/")),
    ]
    if env.get("LOBSTER_DEPLOY_HOST_OVERSEAS"):
        targets.append(
            (
                env["LOBSTER_DEPLOY_HOST_OVERSEAS"],
                env.get("LOBSTER_DEPLOY_REMOTE_DIR_OVERSEAS", "/home/ubuntu/lobster_server").rstrip("/"),
            )
        )

    files = [
        (ROOT / "backend" / "app" / "api" / "auth.py", "backend/app/api/auth.py"),
        (ROOT / "backend" / "app" / "core" / "config.py", "backend/app/core/config.py"),
        (ROOT / "backend" / "app" / "services" / "sms_ihuyi.py", "backend/app/services/sms_ihuyi.py"),
    ]

    pull_update = """
set -e
cd {d}
echo '[deploy] git pull ...'
git fetch origin main
git pull origin main || true
echo '[deploy] server_update_and_restart ...'
bash scripts/server_update_and_restart.sh
"""

    patch_env_restart = """
set -e
cd {d}
ENVF=.env
touch "$ENVF"
sed -i '/^IHUYI_SMS_ACCOUNT=/d;/^IHUYI_SMS_PASSWORD=/d' "$ENVF"
echo 'IHUYI_SMS_ACCOUNT=C21484687' >> "$ENVF"
echo 'IHUYI_SMS_PASSWORD=f0160f84485e884eebb0a017f0f29a1c' >> "$ENVF"
echo '[deploy] IHUYI in .env'
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files --type=service 2>/dev/null | grep -q lobster-backend; then
  sudo systemctl restart lobster-backend lobster-mcp
  sudo systemctl status lobster-backend lobster-mcp --no-pager || true
else
  export PYTHONPATH="$(pwd)"
  [ -f .env ] && set -a && . ./.env && set +a
  PY="$(pwd)/.venv/bin/python"
  pkill -f "backend.run" 2>/dev/null || true
  pkill -f "mcp --port 8001" 2>/dev/null || true
  sleep 2
  nohup "$PY" -m mcp --port "${{MCP_PORT:-8001}}" >> mcp.log 2>&1 &
  sleep 1
  nohup "$PY" -m backend.run >> backend.log 2>&1 &
  sleep 2
fi
echo '[deploy] final restart done'
"""

    for host_line, remote_dir in targets:
        print("===", host_line, remote_dir, "===")
        qd = shlex.quote(remote_dir)
        c = ssh_client_for(host_line, pkey)
        try:
            out, err, code = run_bash(c, pull_update.format(d=qd), timeout=600)
            sys.stdout.write(out)
            if err.strip():
                sys.stderr.write(err)
            if code != 0:
                print("warning: pull/update exit", code, file=sys.stderr)

            sftp = c.open_sftp()
            try:
                for local, rel in files:
                    if not local.is_file():
                        print("missing local", local, file=sys.stderr)
                        return 1
                    rpath = f"{remote_dir}/{rel}"
                    sftp.put(str(local), rpath)
                    print("sftp put", rel)
            finally:
                sftp.close()

            out2, err2, code2 = run_bash(c, patch_env_restart.format(d=qd), timeout=180)
            sys.stdout.write(out2)
            if err2.strip():
                sys.stderr.write(err2)
            if code2 != 0:
                print("error: final step exit", code2, file=sys.stderr)
                return code2
        finally:
            c.close()

    print("OK:", len(targets), "host(s) deployed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
