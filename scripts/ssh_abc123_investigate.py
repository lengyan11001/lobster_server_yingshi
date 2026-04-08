"""查用户 abc123 与最近流水/调用（远端 lobster.db + backend.log 摘取）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ssh_sample_remote_mcp_log as sshutil  # type: ignore

import paramiko


def main() -> int:
    env = sshutil.load_deploy()
    host = env["LOBSTER_DEPLOY_HOST"]
    user, _, hostname = host.partition("@")
    keyp = sshutil.norm_key(env["LOBSTER_DEPLOY_SSH_KEY"])
    remote = env.get("LOBSTER_DEPLOY_REMOTE_DIR", "/root/lobster_server").rstrip("/")
    pp = env.get("LOBSTER_SSH_KEY_PASSPHRASE", "").encode()

    pkey = None
    for Key in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            pkey = Key.from_private_key_file(keyp, password=pp or None)
            break
        except Exception:
            continue
    if not pkey:
        print("key fail", keyp, file=sys.stderr)
        return 1

    sql_users = (
        "SELECT id, email, credits, brand_mark FROM users WHERE "
        "lower(email) LIKE '%abc123%' OR lower(email) LIKE '%abc-123%' "
        "OR email LIKE 'abc123%';"
    )
    # 占位：最近 6 小时流水（UTC 需与库一致；先看最近 50 条全库再找 user）
    sql_recent = (
        "SELECT id, user_id, entry_type, delta, description, datetime(created_at) "
        "FROM credit_ledger ORDER BY id DESC LIMIT 80;"
    )
    grep_log = (
        f"grep -E 'abc123|灌装啤酒|啤酒|image.*video|图生|video' {remote}/backend.log 2>/dev/null | tail -n 60"
    )
    cmd = (
        f"cd {remote} && echo '=== users abc123 ===' && sqlite3 lobster.db '{sql_users}' && "
        f"echo '=== credit_ledger tail ===' && sqlite3 lobster.db -header -column '{sql_recent}'"
    )

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=hostname, username=user, pkey=pkey, timeout=30)
    _, stdout, stderr = c.exec_command(cmd, timeout=120)
    sys.stdout.write(stdout.read().decode("utf-8", errors="replace"))
    err = stderr.read().decode("utf-8", errors="replace")
    if err.strip():
        sys.stderr.write(err)
    _, stdout2, _ = c.exec_command(grep_log, timeout=60)
    sys.stdout.write("\n=== backend.log grep ===\n")
    sys.stdout.write(stdout2.read().decode("utf-8", errors="replace"))
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
