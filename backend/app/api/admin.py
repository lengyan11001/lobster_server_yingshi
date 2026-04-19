"""管理后台：管理员登录、用户查询、积分充值、数据统计。

路由挂载在 /admin 前缀。
- GET  /admin/              管理后台页面
- POST /admin/api/login     管理员登录
- GET  /admin/api/search    搜索用户
- GET  /admin/api/user/{id} 用户详情 + 最近流水
- POST /admin/api/add-credits 给用户加积分
- GET  /admin/api/users     用户列表（分页）
- GET  /admin/api/stats     数据统计
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import cast, func, or_, Date
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from ..models import CapabilityCallLog, CreditLedger, RechargeOrder, User, UserSkillVisibility
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


# ── 技能可见性管理 ──


@router.get("/admin/api/user-skill-visibility/{user_id}")
def admin_get_user_skill_visibility(
    user_id: int,
    _auth: bool = Depends(_verify_admin_token),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    from .skills import _user_visible_package_ids, _load_registry, _pkg_store_visibility, _skill_store_admin
    visible = _user_visible_package_ids(db, user_id)
    registry = _load_registry()
    packages = registry.get("packages", {})
    all_pkgs = [
        {"id": k, "name": v.get("name", k), "store_visibility": _pkg_store_visibility(v)}
        for k, v in packages.items()
    ]
    return {
        "user_id": user_id,
        "is_admin": _skill_store_admin(user),
        "visible_ids": sorted(visible),
        "all_packages": all_pkgs,
    }


class AdminSkillVisUpdate(BaseModel):
    add: list[str] = []
    remove: list[str] = []


@router.post("/admin/api/user-skill-visibility/{user_id}")
def admin_update_user_skill_visibility(
    user_id: int,
    body: AdminSkillVisUpdate,
    _auth: bool = Depends(_verify_admin_token),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    from .skills import _ensure_user_visibility_seeded
    _ensure_user_visibility_seeded(db, user_id)
    added, removed = [], []
    for pkg_id in body.add:
        pkg_id = pkg_id.strip()
        if not pkg_id:
            continue
        exists = db.query(UserSkillVisibility).filter(
            UserSkillVisibility.user_id == user_id,
            UserSkillVisibility.package_id == pkg_id,
        ).first()
        if not exists:
            db.add(UserSkillVisibility(user_id=user_id, package_id=pkg_id))
            added.append(pkg_id)
    for pkg_id in body.remove:
        pkg_id = pkg_id.strip()
        if not pkg_id:
            continue
        row = db.query(UserSkillVisibility).filter(
            UserSkillVisibility.user_id == user_id,
            UserSkillVisibility.package_id == pkg_id,
        ).first()
        if row:
            db.delete(row)
            removed.append(pkg_id)
    db.commit()
    return {"ok": True, "added": added, "removed": removed}


# ── 数据统计 ──


@router.get("/admin/api/stats")
def admin_stats(
    days: int = 30,
    _auth: bool = Depends(_verify_admin_token),
    db: Session = Depends(get_db),
):
    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    range_start = today_start - timedelta(days=max(1, min(days, 90)))

    total_users = db.query(func.count(User.id)).scalar() or 0
    today_new_users = (
        db.query(func.count(User.id))
        .filter(User.created_at >= today_start)
        .scalar() or 0
    )
    total_credits = float(
        db.query(func.coalesce(func.sum(User.credits), 0)).scalar() or 0
    )

    today_recharge_paid = float(
        db.query(func.coalesce(func.sum(RechargeOrder.credits), 0))
        .filter(
            RechargeOrder.status == "paid",
            RechargeOrder.paid_at >= today_start,
        )
        .scalar() or 0
    )

    today_recharge_admin = float(
        db.query(func.coalesce(func.sum(CreditLedger.delta), 0))
        .filter(
            CreditLedger.entry_type == "recharge",
            CreditLedger.description.like("%管理员%"),
            CreditLedger.created_at >= today_start,
        )
        .scalar() or 0
    )

    today_consume = float(
        db.query(func.coalesce(func.sum(CreditLedger.delta), 0))
        .filter(
            CreditLedger.entry_type.in_(["sutui_chat", "pre_deduct", "settle", "unit_deduct"]),
            CreditLedger.delta < 0,
            CreditLedger.created_at >= today_start,
        )
        .scalar() or 0
    )

    paid_orders_today = (
        db.query(func.count(RechargeOrder.id))
        .filter(
            RechargeOrder.status == "paid",
            RechargeOrder.paid_at >= today_start,
        )
        .scalar() or 0
    )

    total_paid_revenue_fen = (
        db.query(func.coalesce(func.sum(RechargeOrder.callback_amount_fen), 0))
        .filter(RechargeOrder.status == "paid")
        .scalar() or 0
    )

    date_col = func.date(User.created_at)
    daily_users_raw = (
        db.query(date_col.label("d"), func.count(User.id).label("cnt"))
        .filter(User.created_at >= range_start)
        .group_by(date_col)
        .order_by(date_col)
        .all()
    )
    daily_users = [{"date": str(r.d), "count": r.cnt} for r in daily_users_raw]

    order_date = func.date(RechargeOrder.paid_at)
    daily_recharge_raw = (
        db.query(order_date.label("d"), func.sum(RechargeOrder.credits).label("total"))
        .filter(
            RechargeOrder.status == "paid",
            RechargeOrder.paid_at >= range_start,
        )
        .group_by(order_date)
        .order_by(order_date)
        .all()
    )
    daily_recharge = [{"date": str(r.d), "amount": float(r.total)} for r in daily_recharge_raw]

    ledger_date = func.date(CreditLedger.created_at)

    daily_consume_raw = (
        db.query(ledger_date.label("d"), func.sum(CreditLedger.delta).label("total"))
        .filter(
            CreditLedger.entry_type.in_(["sutui_chat", "pre_deduct", "settle", "unit_deduct"]),
            CreditLedger.delta < 0,
            CreditLedger.created_at >= range_start,
        )
        .group_by(ledger_date)
        .order_by(ledger_date)
        .all()
    )
    daily_consume = [{"date": str(r.d), "amount": abs(float(r.total))} for r in daily_consume_raw]

    cap_ranking_raw = (
        db.query(
            CapabilityCallLog.capability_id,
            func.count(CapabilityCallLog.id).label("calls"),
            func.sum(CapabilityCallLog.credits_charged).label("credits"),
        )
        .filter(CapabilityCallLog.created_at >= range_start)
        .group_by(CapabilityCallLog.capability_id)
        .order_by(func.count(CapabilityCallLog.id).desc())
        .limit(10)
        .all()
    )
    capability_ranking = [
        {"capability_id": r.capability_id, "calls": r.calls, "credits": float(r.credits or 0)}
        for r in cap_ranking_raw
    ]

    top_consumers_raw = (
        db.query(
            CreditLedger.user_id,
            func.sum(CreditLedger.delta).label("total_consumed"),
        )
        .filter(
            CreditLedger.delta < 0,
            CreditLedger.created_at >= range_start,
        )
        .group_by(CreditLedger.user_id)
        .order_by(func.sum(CreditLedger.delta))
        .limit(10)
        .all()
    )
    top_consumer_ids = [r.user_id for r in top_consumers_raw]
    user_map = {}
    if top_consumer_ids:
        for u in db.query(User).filter(User.id.in_(top_consumer_ids)).all():
            user_map[u.id] = u.email
    top_consumers = [
        {
            "user_id": r.user_id,
            "email": user_map.get(r.user_id, "?"),
            "consumed": abs(float(r.total_consumed)),
        }
        for r in top_consumers_raw
    ]

    return {
        "overview": {
            "total_users": total_users,
            "today_new_users": today_new_users,
            "total_credits_pool": round(total_credits, 2),
            "today_recharge_paid": round(today_recharge_paid, 2),
            "today_recharge_admin": round(today_recharge_admin, 2),
            "today_consume": round(abs(today_consume), 2),
            "paid_orders_today": paid_orders_today,
            "total_revenue_yuan": round(int(total_paid_revenue_fen) / 100, 2),
        },
        "daily_users": daily_users,
        "daily_recharge": daily_recharge,
        "daily_consume": daily_consume,
        "capability_ranking": capability_ranking,
        "top_consumers": top_consumers,
    }


# ============================================================================
# TikHub 管理：Token、定价、调用日志、目录刷新
# ============================================================================

import json as _tk_json
import os as _tk_os


def _tk_env_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent / ".env"


def _tk_pricing_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent / "tikhub_pricing.json"


def _tk_mask(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 10:
        return "*" * len(token)
    return token[:6] + "*" * 6 + token[-4:]


def _tk_update_env_token(new_token: str) -> None:
    """把 .env 里的 TIKHUB_API_KEY 替换成新值；不存在则追加。同时更新进程内 settings 缓存。"""
    p = _tk_env_path()
    lines: list[str] = []
    if p.exists():
        lines = p.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("TIKHUB_API_KEY=") or s.startswith("# TIKHUB_API_KEY="):
            lines[i] = f"TIKHUB_API_KEY={new_token}"
            found = True
            break
    if not found:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("# TikHub 多平台数据代理：服务端统一持有的 Bearer Token")
        lines.append(f"TIKHUB_API_KEY={new_token}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    settings.tikhub_api_key = new_token
    _tk_os.environ["TIKHUB_API_KEY"] = new_token


@router.get("/admin/api/tikhub/config")
def admin_tikhub_config(_auth: bool = Depends(_verify_admin_token)):
    token = (settings.tikhub_api_key or "").strip()
    return {
        "configured": bool(token),
        "token_masked": _tk_mask(token),
        "api_base": settings.tikhub_api_base,
    }


class _TkSaveBody(BaseModel):
    token: str


@router.post("/admin/api/tikhub/config")
def admin_tikhub_save_config(body: _TkSaveBody, _auth: bool = Depends(_verify_admin_token)):
    new_token = (body.token or "").strip()
    if not new_token:
        raise HTTPException(400, "token 不能为空")
    _tk_update_env_token(new_token)
    return {"ok": True, "token_masked": _tk_mask(new_token)}


@router.post("/admin/api/tikhub/test")
async def admin_tikhub_test(_auth: bool = Depends(_verify_admin_token)):
    """测试 Token：调用一次 TikHub 用户信息接口验活，不计费、不入库。"""
    import httpx as _httpx
    token = (settings.tikhub_api_key or "").strip()
    if not token:
        raise HTTPException(400, "尚未配置 Token")
    base = (settings.tikhub_api_base or "https://api.tikhub.io").rstrip("/")
    url = base + "/api/v1/tikhub/user/get_user_info"
    try:
        async with _httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "detail": (r.text or "")[:300]}
        data = r.json() if r.content else {}
        return {"ok": True, "status": r.status_code, "data": data}
    except Exception as e:
        return {"ok": False, "status": 0, "detail": str(e)[:300]}


@router.get("/admin/api/tikhub/pricing")
def admin_tikhub_get_pricing(_auth: bool = Depends(_verify_admin_token)):
    p = _tk_pricing_path()
    if not p.exists():
        return {"default_unit_credits": 1, "platforms": {}, "overrides": {}}
    try:
        return _tk_json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"读取定价表失败: {e}")


class _TkPricingBody(BaseModel):
    default_unit_credits: int = 1
    platforms: dict[str, dict[str, int]] = {}
    overrides: dict[str, dict[str, int]] = {}


@router.post("/admin/api/tikhub/pricing")
def admin_tikhub_save_pricing(body: _TkPricingBody, _auth: bool = Depends(_verify_admin_token)):
    p = _tk_pricing_path()
    out = {
        "default_unit_credits": max(0, int(body.default_unit_credits or 1)),
        "platforms": {k: {"unit_credits": max(0, int(v.get("unit_credits", 1)))} for k, v in (body.platforms or {}).items()},
        "overrides": {k: {"unit_credits": max(0, int(v.get("unit_credits", 1)))} for k, v in (body.overrides or {}).items()},
    }
    p.write_text(_tk_json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "saved": out}


@router.get("/admin/api/tikhub/logs")
def admin_tikhub_logs(
    limit: int = 100,
    _auth: bool = Depends(_verify_admin_token),
    db: Session = Depends(get_db),
):
    limit = max(1, min(500, int(limit)))
    rows = (
        db.query(CapabilityCallLog)
        .filter(CapabilityCallLog.capability_id == "tikhub.fetch")
        .order_by(CapabilityCallLog.id.desc())
        .limit(limit)
        .all()
    )
    user_ids = {r.user_id for r in rows}
    name_map: dict[int, str] = {}
    if user_ids:
        for u in db.query(User).filter(User.id.in_(list(user_ids))).all():
            name_map[u.id] = u.email or str(u.id)
    out = []
    for r in rows:
        out.append({
            "id": r.id,
            "user_id": r.user_id,
            "user": name_map.get(r.user_id, str(r.user_id)),
            "endpoint_id": r.upstream_tool,
            "success": bool(r.success),
            "credits": float(r.credits_charged or 0),
            "latency_ms": r.latency_ms,
            "status": r.status,
            "error": (r.error_message or "")[:200],
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    # 汇总
    total = len(rows)
    total_credits = sum((float(r.credits_charged or 0) for r in rows), 0.0)
    success_cnt = sum(1 for r in rows if r.success)
    return {
        "logs": out,
        "summary": {
            "shown": total,
            "success": success_cnt,
            "failed": total - success_cnt,
            "total_credits": round(total_credits, 4),
        },
    }


@router.get("/admin/api/tikhub/catalog-meta")
def admin_tikhub_catalog_meta(_auth: bool = Depends(_verify_admin_token)):
    from .tikhub_proxy import _load_catalog
    cat = _load_catalog()
    plats = []
    for p in cat.get("platforms", []):
        ep_count = sum(len(g.get("endpoints", [])) for g in p.get("groups", []))
        plats.append({"id": p.get("id"), "name": p.get("name"), "icon": p.get("icon"), "endpoints": ep_count})
    return {
        "platform_count": cat.get("platform_count"),
        "endpoint_count": cat.get("endpoint_count"),
        "generated_at": cat.get("generated_at"),
        "platforms": plats,
    }


@router.post("/admin/api/tikhub/refresh-catalog")
def admin_tikhub_refresh(_auth: bool = Depends(_verify_admin_token)):
    from .tikhub_proxy import refresh_catalog_blocking
    try:
        meta = refresh_catalog_blocking(force=True)
        return {"ok": True, **meta}
    except Exception as e:
        raise HTTPException(500, f"刷新失败: {e}")
