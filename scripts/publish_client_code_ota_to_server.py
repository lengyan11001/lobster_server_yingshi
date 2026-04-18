#!/usr/bin/env python3
"""用 .env.deploy SSH 将本地 OTA zip 上传到大陆机 client_static/client_code/bundles/ 并写入 manifest.json。"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
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
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path", type=Path, help="本地 OTA zip")
    ap.add_argument("--version", default="1.0.5", help="manifest.version")
    ap.add_argument("--build", type=int, default=5, help="manifest.build（须大于客户端 CLIENT_CODE_VERSION.build 才会强拉）")
    ap.add_argument(
        "--public-base",
        default="https://bhzn.top",
        help="manifest.bundle_url 使用的 API 根（与线上一致）",
    )
    args = ap.parse_args()

    z = args.zip_path.resolve()
    if not z.is_file():
        print(f"[ERR] 文件不存在: {z}", file=sys.stderr)
        return 2

    h = hashlib.sha256()
    with z.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    sha = h.hexdigest()

    env = load_deploy()
    host = env["LOBSTER_DEPLOY_HOST"]
    user, _, hostname = host.partition("@")
    keyp = norm_key(env["LOBSTER_DEPLOY_SSH_KEY"])
    remote_root = env.get("LOBSTER_DEPLOY_REMOTE_DIR", "/root/lobster_server").rstrip("/")
    pp = env.get("LOBSTER_SSH_KEY_PASSPHRASE", "").encode()

    bundle_name = z.name
    bundle_url = f"{args.public_base.rstrip('/')}/client/client-code/bundles/{bundle_name}"
    manifest = {
        "version": args.version,
        "build": args.build,
        "bundle_url": bundle_url,
        "sha256": sha,
        "note": f"OTA {bundle_name}; paths 省略用客户端 DEFAULT_PATHS",
    }
    remote_base = f"{remote_root}/client_static/client_code"
    remote_bundles = f"{remote_base}/bundles"
    remote_zip = f"{remote_bundles}/{bundle_name}"
    remote_manifest = f"{remote_base}/manifest.json"

    import paramiko

    pkey = None
    for Key in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            pkey = Key.from_private_key_file(keyp, password=pp or None)
            break
        except Exception:
            continue
    if not pkey:
        print("could not load key:", keyp, file=sys.stderr)
        return 1

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=hostname, username=user, pkey=pkey, timeout=45)

    for cmd in (
        f"mkdir -p {remote_bundles}",
    ):
        _, stdout, stderr = c.exec_command(cmd, timeout=60)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if err.strip():
            print(err, file=sys.stderr)

    sftp = c.open_sftp()
    try:
        sftp.put(str(z), remote_zip)
    finally:
        sftp.close()

    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    sftp = c.open_sftp()
    try:
        with sftp.file(remote_manifest, "w") as rf:
            rf.write(payload.encode("utf-8"))
    finally:
        sftp.close()

    c.close()

    print("[OK] 已上传:", remote_zip)
    print("[OK] 已写入:", remote_manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
