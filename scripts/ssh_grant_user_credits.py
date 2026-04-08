#!/usr/bin/env python3
"""SSH 到 .env.deploy 主机，给用户邮箱增加积分并写 credit_ledger（与 append_credit_ledger 一致）。"""
from __future__ import annotations

import argparse
import base64
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
    ap.add_argument("email", help="users.email")
    ap.add_argument("delta", type=str, help="增加积分数，如 2000")
    ap.add_argument("--ref-id", default="admin-grant", help="credit_ledger.ref_id 前缀")
    args = ap.parse_args()

    env = load_deploy()
    host = env["LOBSTER_DEPLOY_HOST"]
    user, _, hostname = host.partition("@")
    keyp = norm_key(env["LOBSTER_DEPLOY_SSH_KEY"])
    remote = env.get("LOBSTER_DEPLOY_REMOTE_DIR", "/root/lobster_server").rstrip("/")
    pp = env.get("LOBSTER_SSH_KEY_PASSPHRASE", "").encode()

    email = (args.email or "").strip().replace("'", "\\'")
    delta = (args.delta or "").strip()
    ref_id = (args.ref_id or "admin-grant").strip().replace("'", "\\'")

    # 远端执行：base64 解码后交 python，避免引号地狱
    py = f"""from decimal import Decimal
import os, sys
os.chdir("{remote}")
sys.path.insert(0, "{remote}")
from dotenv import load_dotenv
load_dotenv("{remote}/.env", override=False)
from backend.app.db import SessionLocal
from backend.app.models import User
from backend.app.services.credit_ledger import append_credit_ledger
EMAIL = {args.email!r}
ADD = Decimal({delta!r})
REF = {ref_id!r}
db = SessionLocal()
try:
    u = db.query(User).filter(User.email == EMAIL).first()
    if not u:
        print("ERR user not found:", EMAIL)
        sys.exit(1)
    old = u.credits
    u.credits = old + ADD
    append_credit_ledger(
        db,
        int(u.id),
        ADD,
        "recharge",
        u.credits,
        description="运营手工加积分 +" + str(ADD),
        ref_type="manual",
        ref_id=REF,
        meta={{"source": "ssh_grant_user_credits"}},
    )
    db.commit()
    print("OK user_id=", u.id, "email=", u.email, "credits", old, "->", u.credits)
finally:
    db.close()
"""
    b64 = base64.b64encode(py.encode("utf-8")).decode("ascii")
    cmd = (
        f"bash -lc 'cd {remote} && export PYTHONPATH=. && "
        f"if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi && "
        f"echo {b64} | base64 -d | $PY'"
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
