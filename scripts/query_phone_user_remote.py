"""SSH 到 .env.deploy 大陆机，按手机号查 users / user_installations 等。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
PHONE = sys.argv[1] if len(sys.argv) > 1 else "18124655127"


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
        print("cannot load key", keyp, file=sys.stderr)
        return 1

    host_line = env["LOBSTER_DEPLOY_HOST"]
    user, _, hostname = host_line.partition("@")
    rdir = env.get("LOBSTER_DEPLOY_REMOTE_DIR", "/root/lobster_server").rstrip("/")

    # 远端 heredoc，避免 Windows 引号地狱
    remote = f"""set -e
cd {rdir}
.venv/bin/python <<'PY'
import os, sys
os.chdir({rdir!r})
sys.path.insert(0, {rdir!r})
from dotenv import load_dotenv
load_dotenv(os.path.join({rdir!r}, ".env"))
from backend.app.db import SessionLocal
from backend.app.models import User, UserInstallation, InstallationSignupBonusClaim, SkillUnlock

phone = {PHONE!r}
email = phone + "@sms.lobster.local"
db = SessionLocal()
u = db.query(User).filter(User.email == email).first()
if not u:
    u = db.query(User).filter(User.email.like("%" + phone + "%")).first()
if not u:
    print("NOT_FOUND for", email)
    sys.exit(0)
print("=== users ===")
print("id:", u.id)
print("email:", u.email)
print("role:", u.role)
print("credits:", u.credits)
print("preferred_model:", u.preferred_model)
print("brand_mark:", u.brand_mark)
print("created_at:", u.created_at)
print("wechat_openid:", u.wechat_openid)
print("=== user_installations ===")
rows = db.query(UserInstallation).filter(UserInstallation.user_id == u.id).order_by(UserInstallation.created_at).all()
if not rows:
    print("(none)")
for r in rows:
    print("installation_id:", r.installation_id)
    print("  last_seen_at:", r.last_seen_at)
    print("  created_at:", r.created_at)
print("=== installation_signup_bonus_claims (this user) ===")
rows2 = db.query(InstallationSignupBonusClaim).filter(InstallationSignupBonusClaim.user_id == u.id).all()
if not rows2:
    print("(none)")
for r in rows2:
    print("installation_id:", r.installation_id, "claimed_at:", r.created_at)
print("=== skill_unlocks ===")
for r in db.query(SkillUnlock).filter(SkillUnlock.user_id == u.id).all():
    print(r.package_id, r.unlocked_at)
db.close()
PY
"""

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=hostname, username=user, pkey=pkey, timeout=60)
    try:
        stdin, stdout, stderr = c.exec_command("bash -s", timeout=120)
        stdin.write(remote)
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        sys.stdout.write(out)
        if err.strip():
            sys.stderr.write(err)
        return stdout.channel.recv_exit_status() or 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
