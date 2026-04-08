"""SSH 到 .env.deploy 配置的主机，查 lobster.db 里 credit_ledger 的 sutui_chat 记录。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 复用 ssh_sample_remote_mcp_log 的加载与连接
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ssh_sample_remote_mcp_log as sshutil  # type: ignore


def main() -> int:
    env = sshutil.load_deploy()
    host = env["LOBSTER_DEPLOY_HOST"]
    user, _, hostname = host.partition("@")
    keyp = sshutil.norm_key(env["LOBSTER_DEPLOY_SSH_KEY"])
    remote = env.get("LOBSTER_DEPLOY_REMOTE_DIR", "/root/lobster_server").rstrip("/")
    pp = env.get("LOBSTER_SSH_KEY_PASSPHRASE", "").encode()

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

    sql1 = "SELECT entry_type, COUNT(*) FROM credit_ledger GROUP BY entry_type ORDER BY 2 DESC;"
    sql2 = (
        "SELECT id, user_id, entry_type, delta, description, created_at FROM credit_ledger "
        "WHERE entry_type='sutui_chat' ORDER BY id DESC LIMIT 20;"
    )
    # bash：整条 SQL 用双引号包住，字面量里的单引号不会截断
    cmd = (
        f"cd {remote} && echo '=== entry_type counts ===' && sqlite3 lobster.db \"{sql1}\" "
        f"&& echo '=== latest sutui_chat ===' && sqlite3 lobster.db -header -column \"{sql2}\""
    )

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
