#!/usr/bin/env python3
"""
在 lobster_server 仓库根目录执行（与 systemd / server_update 一致）：
  cd /root/lobster_server && export PYTHONPATH="$PWD" && .venv/bin/python scripts/set_user_admin_and_credits.py mdlr88888 --add-credits 1000

将用户 role 设为 admin，并在现有 credits 上增加指定数额（Decimal）。
"""
from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)


def main() -> None:
    from backend.app.db import SessionLocal
    from backend.app.models import User
    from sqlalchemy import or_

    p = argparse.ArgumentParser(description="设用户为 admin 并增加积分")
    p.add_argument("email_or_prefix", help="users.email 全量或前缀（如 mdlr88888 可匹配 mdlr88888@xxx）")
    p.add_argument("--add-credits", default="1000", help="在现有 credits 上增加（默认 1000）")
    args = p.parse_args()
    needle = (args.email_or_prefix or "").strip()
    if not needle:
        print("ERR: 空邮箱", file=sys.stderr)
        sys.exit(1)
    add = Decimal(str(args.add_credits))

    db = SessionLocal()
    try:
        u = (
            db.query(User)
            .filter(
                or_(
                    User.email == needle,
                    User.email.like(needle + "@%"),
                )
            )
            .order_by(User.id.asc())
            .first()
        )
        if not u:
            print("ERR: 未找到用户（email 全匹配或 前缀@）:", needle, file=sys.stderr)
            sys.exit(1)
        before_role, before_credits = u.role, u.credits
        u.role = "admin"
        u.credits = (u.credits or Decimal(0)) + add
        db.commit()
        print("OK id=%s email=%s role %s -> admin credits %s -> %s" % (u.id, u.email, before_role, before_credits, u.credits))
    finally:
        db.close()


if __name__ == "__main__":
    main()
