#!/usr/bin/env python3
"""把本地安装包 zip 上传到服务器 landing_private/，对应 /api/landing/download 转发的私有目录。

用法：
    python scripts/upload_landing_zip.py <local_zip> [<remote_filename>]

remote_filename 缺省时取 local_zip 的 basename。

凭证从 .env.deploy 读取（与 publish_client_code_ota_to_server.py 一致）。
landing_private/ 目录里的 zip 不通过 StaticFiles 公开，仅付款后凭 download_token 转发。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
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
    """Git Bash 习惯的 /d/maczhuji → D:\\maczhuji。"""
    p = path.strip()
    if len(p) >= 3 and p[0] == "/" and p[2] == "/":
        return p[1].upper() + ":" + p[2:].replace("/", os.sep)
    return p.replace("/", os.sep)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("local_zip", type=Path)
    ap.add_argument("remote_filename", nargs="?", default=None,
                    help="缺省取 local_zip 的 basename。须与 backend/app/api/landing_pay.py 的 INSCLAW_FILE_VARIANTS 配置一致。")
    args = ap.parse_args()

    src = args.local_zip.resolve()
    if not src.is_file():
        print(f"[ERR] 本地文件不存在: {src}", file=sys.stderr)
        return 2
    remote_name = args.remote_filename or src.name

    env = load_deploy()
    host = env["LOBSTER_DEPLOY_HOST"]
    user, _, hostname = host.partition("@")
    keyp = norm_key(env["LOBSTER_DEPLOY_SSH_KEY"])
    remote_root = env.get("LOBSTER_DEPLOY_REMOTE_DIR", "/root/lobster_server").rstrip("/")
    pp = env.get("LOBSTER_SSH_KEY_PASSPHRASE", "").encode()

    remote_dir = f"{remote_root}/landing_private"
    remote_path = f"{remote_dir}/{remote_name}"

    import paramiko

    pkey = None
    last_err = None
    for Key in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            pkey = Key.from_private_key_file(keyp, password=pp or None)
            break
        except Exception as e:
            last_err = e
            continue
    if not pkey:
        print(f"[ERR] could not load key {keyp}: {last_err}", file=sys.stderr)
        return 1

    print(f"[upload] {src} ({src.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"      → {host}:{remote_path}")

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=hostname, username=user, pkey=pkey, timeout=45)
    _, stdout, stderr = c.exec_command(f"mkdir -p {remote_dir}", timeout=30)
    err = stderr.read().decode("utf-8", errors="replace")
    if err.strip():
        print(err, file=sys.stderr)

    sftp = c.open_sftp()
    try:
        last = [0.0, time.time(), 0]

        def _cb(transferred: int, total: int) -> None:
            now = time.time()
            if now - last[1] >= 2.0 or transferred == total:
                pct = transferred * 100.0 / total if total else 0.0
                spd = (transferred - last[2]) / (1024 * 1024) / max(0.001, now - last[1])
                print(f"  {transferred / 1024 / 1024:8.1f} / {total / 1024 / 1024:.1f} MB  ({pct:5.1f}%)  {spd:6.2f} MB/s")
                last[1] = now
                last[2] = transferred

        sftp.put(str(src), remote_path, callback=_cb)
        attrs = sftp.stat(remote_path)
        print(f"[OK] uploaded, remote size = {attrs.st_size / 1024 / 1024:.1f} MB")
    finally:
        sftp.close()
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
