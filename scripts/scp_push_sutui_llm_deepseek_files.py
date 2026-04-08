#!/usr/bin/env python3
"""同步 api/sutui_llm.py 与 services/sutui_llm_probe.py 至大陆机并重启（git push 受阻时的补救）。"""
from __future__ import annotations

import os
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


def main() -> int:
    env = load_deploy()
    host = env["LOBSTER_DEPLOY_HOST"]
    user, _, hostname = host.partition("@")
    keyp = norm_key(env["LOBSTER_DEPLOY_SSH_KEY"])
    remote_root = env.get("LOBSTER_DEPLOY_REMOTE_DIR", "/root/lobster_server").rstrip("/")
    pp = env.get("LOBSTER_SSH_KEY_PASSPHRASE", "").encode() or None

    import paramiko

    pkey = None
    for Key in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            pkey = Key.from_private_key_file(keyp, password=pp)
            break
        except Exception:
            continue
    if not pkey:
        print("[ERR] 无法加载私钥", keyp, file=sys.stderr)
        return 1

    pairs = [
        (ROOT / "backend/app/api/sutui_llm.py", f"{remote_root}/backend/app/api/sutui_llm.py"),
        (ROOT / "backend/app/services/sutui_llm_probe.py", f"{remote_root}/backend/app/services/sutui_llm_probe.py"),
    ]
    for src, _ in pairs:
        if not src.is_file():
            print("[ERR] 本地缺少", src, file=sys.stderr)
            return 2

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=hostname, username=user, pkey=pkey, timeout=45)
    try:
        sftp = c.open_sftp()
        try:
            for src, dst in pairs:
                sftp.put(str(src), dst)
                print("[OK] put", dst)
        finally:
            sftp.close()

        _, stdout, stderr = c.exec_command(
            "cd "
            + remote_root
            + " && export PYTHONPATH="
            + remote_root
            + " && [ -f .env ] && set -a && . ./.env && set +a; "
            + "PY="
            + remote_root
            + "/.venv/bin/python; "
            + "pkill -f backend.run 2>/dev/null || true; pkill -f 'mcp --port' 2>/dev/null || true; "
            + "sleep 2; "
            + "nohup $PY -m mcp --port ${MCP_PORT:-8001} >> mcp.log 2>&1 & "
            + "sleep 1; nohup $PY -m backend.run >> backend.log 2>&1 & "
            + "sleep 1; echo restarted",
            timeout=90,
        )
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        print(out or "")
        if err.strip():
            print(err, file=sys.stderr)
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
