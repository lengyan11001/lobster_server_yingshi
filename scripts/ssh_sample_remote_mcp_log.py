"""
用本机 lobster-server/.env.deploy 里的 SSH 配置连接 ECS（含加密私钥口令 LOBSTER_SSH_KEY_PASSPHRASE），
在远端执行一段 shell，默认打印 mcp.log 里含 transfer_url 的行。

依赖：pip install paramiko

用法：
  python scripts/ssh_sample_remote_mcp_log.py
  python scripts/ssh_sample_remote_mcp_log.py "tail -n 30 /root/lobster_server/mcp.log"
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def load_deploy() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    d: dict[str, str] = {}
    for line in (root / ".env.deploy").read_text(encoding="utf-8").splitlines():
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


def main() -> int:
    env = load_deploy()
    host = env["LOBSTER_DEPLOY_HOST"]
    user, _, hostname = host.partition("@")
    keyp = norm_key(env["LOBSTER_DEPLOY_SSH_KEY"])
    remote = env.get("LOBSTER_DEPLOY_REMOTE_DIR", "/root/lobster_server").rstrip("/")
    pp = env.get("LOBSTER_SSH_KEY_PASSPHRASE", "").encode()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
    else:
        cmd = (
            f"grep -hn 'transfer_url' {remote}/mcp.log 2>/dev/null | tail -n 20"
        )

    import paramiko

    pkey = None
    for Key in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            pkey = Key.from_private_key_file(keyp, password=pp or None)
            break
        except Exception:
            continue
    if not pkey:
        print("could not load private key:", keyp, file=sys.stderr)
        return 1

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=hostname, username=user, pkey=pkey, timeout=30)
    _, stdout, stderr = c.exec_command(cmd, timeout=120)
    sys.stdout.write(stdout.read().decode("utf-8", errors="replace"))
    err = stderr.read().decode("utf-8", errors="replace")
    if err.strip():
        sys.stderr.write(err)
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
