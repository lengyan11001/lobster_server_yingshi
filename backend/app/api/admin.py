"""管理后台：管理员登录、用户查询、积分充值。

路由挂载在 /admin 前缀。
- GET  /admin/              管理后台页面
- POST /admin/api/login     管理员登录
- GET  /admin/api/search    搜索用户
- GET  /admin/api/user/{id} 用户详情 + 最近流水
- POST /admin/api/add-credits 给用户加积分
- GET  /admin/api/users     用户列表（分页）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from ..models import CreditLedger, User
from ..services.credit_ledger import append_credit_ledger
from ..services.credits_amount import quantize_credits

router = APIRouter()
logger = logging.getLogger(__name__)

ADMIN_TOKEN_PREFIX = "lobster-admin-"


def _admin_enabled() -> bool:
    return bool((settings.lobster_admin_username or "").strip() and (settings.lobster_admin_password or "").strip())


def _verify_admin_token(x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token")):
    if not _admin_enabled():
        raise HTTPException(status_code=503, detail="管理后台未配置")
    expected = ADMIN_TOKEN_PREFIX + (settings.lobster_admin_password or "").strip()
    if not x_admin_token or x_admin_token.strip() != expected:
        raise HTTPException(status_code=401, detail="管理员凭证无效")
    return True


# ── 页面 ──

@router.get("/admin", include_in_schema=False)
@router.get("/admin/", include_in_schema=False)
def admin_page():
    html_path = Path(__file__).resolve().parent.parent / "static" / "admin.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="管理后台页面未找到")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ── API ──

class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/admin/api/login")
def admin_login(body: LoginBody):
    if not _admin_enabled():
        raise HTTPException(status_code=503, detail="管理后台未配置，请在 .env 设置 LOBSTER_ADMIN_USERNAME 和 LOBSTER_ADMIN_PASSWORD")
    u = (settings.lobster_admin_username or "").strip()
    p = (settings.lobster_admin_password or "").strip()
    if body.username.strip() != u or body.password.strip() != p:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = ADMIN_TOKEN_PREFIX + p
    return {"ok": True, "token": token}


@router.get("/admin/api/search")
def admin_search_user(
    q: str = "",
    _auth: bool = Depends(_verify_admin_token),
    db: Session = Depends(get_db),
):
    q = q.strip()
    if not q:
        return {"users": []}
    query = db.query(User).filter(
        or_(
            User.email.ilike(f"%{q}%"),
            User.id == int(q) if q.isdigit() else False,
        )
    ).order_by(User.id).limit(50)
    users = []
    for u in query.all():
        users.append({
            "id": u.id,
            "email": u.email,
            "credits": float(u.credits or 0),
            "role": u.role,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })
    return {"users": users}


@router.get("/admin/api/user/{user_id}")
def admin_user_detail(
    user_id: int,
    _auth: bool = Depends(_verify_admin_token),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    ledger = (
        db.query(CreditLedger)
        .filter(CreditLedger.user_id == user_id)
        .order_by(CreditLedger.created_at.desc())
        .limit(50)
        .all()
    )
    ledger_list = []
    for entry in ledger:
        ledger_list.append({
            "id": entry.id,
            "delta": float(entry.delta),
            "balance_after": float(entry.balance_after),
            "entry_type": entry.entry_type,
            "description": entry.description,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        })
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "credits": float(user.credits or 0),
            "role": user.role,
            "is_agent": user.is_agent,
            "brand_mark": user.brand_mark,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "ledger": ledger_list,
    }


class AddCreditsBody(BaseModel):
    user_id: int
    amount: float
    description: str = "管理员手动加积分"


@router.post("/admin/api/add-credits")
def admin_add_credits(
    body: AddCreditsBody,
    _auth: bool = Depends(_verify_admin_token),
    db: Session = Depends(get_db),
):
    if body.amount == 0:
        raise HTTPException(status_code=400, detail="积分数量不能为 0")
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    old_credits = quantize_credits(user.credits or 0)
    delta = quantize_credits(body.amount)
    new_credits = old_credits + delta
    if new_credits < 0:
        raise HTTPException(status_code=400, detail=f"积分不足，当前 {old_credits}，操作 {delta}")
    user.credits = new_credits

    append_credit_ledger(
        db,
        user.id,
        delta,
        "recharge",
        new_credits,
        description=body.description[:200],
        meta={"source": "admin_panel"},
    )
    db.commit()
    db.refresh(user)

    return {
        "ok": True,
        "user_id": user.id,
        "email": user.email,
        "old_credits": float(old_credits),
        "new_credits": float(quantize_credits(user.credits)),
        "delta": float(delta),
    }


@router.get("/admin/api/users")
def admin_list_users(
    page: int = 1,
    page_size: int = 20,
    _auth: bool = Depends(_verify_admin_token),
    db: Session = Depends(get_db),
):
    total = db.query(func.count(User.id)).scalar() or 0
    offset = (max(1, page) - 1) * page_size
    users = db.query(User).order_by(User.id.desc()).offset(offset).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "users": [
            {
                "id": u.id,
                "email": u.email,
                "credits": float(u.credits or 0),
                "role": u.role,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
    }
