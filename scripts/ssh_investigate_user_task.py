"""远端：按关键词查用户、最近流水、capability/tool 日志（避免 PowerShell 引号问题）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ssh_sample_remote_mcp_log as sshutil  # type: ignore

import paramiko


def _sql_commands(remote: str) -> str:
    # 每条 sqlite 用 <<-SQ 在远端执行，避免引号地狱
    return r"""set -e
cd REMOTE_DIR
DB=lobster.db
echo '=== users match abc123 ==='
sqlite3 "$DB" "SELECT id, email, credits, brand_mark FROM users WHERE lower(email) LIKE '%abc123%' OR lower(email) LIKE '%灌装%' LIMIT 30;" || true
echo '=== recent capability_call_logs (video/image) ==='
sqlite3 "$DB" -header -column "SELECT id, user_id, capability_id, success, credits_charged, status, datetime(created_at) FROM capability_call_logs ORDER BY id DESC LIMIT 25;" || true
echo '=== capability with beer in payload (json) last 2h-ish ==='
sqlite3 "$DB" "SELECT id, user_id, capability_id, substr(ifnull(request_payload,''),1,200), datetime(created_at) FROM capability_call_logs WHERE request_payload LIKE '%啤酒%' OR request_payload LIKE '%罐%' ORDER BY id DESC LIMIT 15;" || true
echo '=== credit_ledger pre_deduct last 30 ==='
sqlite3 "$DB" -header -column "SELECT id, user_id, entry_type, delta, substr(description,1,100), datetime(created_at) FROM credit_ledger WHERE entry_type IN ('pre_deduct','settle','direct_charge') ORDER BY id DESC LIMIT 30;" || true
echo '=== backend.log: abc123 ==='
grep -F 'abc123' backend.log 2>/dev/null | tail -n 25 || true
echo '=== backend.log: 啤酒 ==='
grep -F '啤酒' backend.log 2>/dev/null | tail -n 25 || true
""".replace("REMOTE_DIR", remote)


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
        print("key fail", file=sys.stderr)
        return 1

    script = _sql_commands(remote)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=hostname, username=user, pkey=pkey, timeout=30)
    _, stdout, stderr = c.exec_command(f"bash -s <<'INV_EOF'\n{script}\nINV_EOF", timeout=180)
    sys.stdout.write(stdout.read().decode("utf-8", errors="replace"))
    err = stderr.read().decode("utf-8", errors="replace")
    if err.strip():
        sys.stderr.write(err)
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
