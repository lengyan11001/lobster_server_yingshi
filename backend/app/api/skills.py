"""Skill/MCP package management: install, uninstall, list store；技能付费解锁。"""
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from .auth import get_current_user
from ..models import CapabilityConfig, SkillUnlock, SkillUnlockOrder, User

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent


def _load_registry() -> dict:
    p = _BASE_DIR / "skill_registry.json"
    if not p.exists():
        return {"packages": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"packages": {}}


def _load_installed() -> dict:
    p = _BASE_DIR / "installed_packages.json"
    if not p.exists():
        return {"installed": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"installed": []}


def _save_installed(data: dict):
    p = _BASE_DIR / "installed_packages.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_local_catalog() -> dict:
    p = _BASE_DIR / "mcp" / "capability_catalog.local.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_local_catalog(catalog: dict):
    p = _BASE_DIR / "mcp" / "capability_catalog.local.json"
    p.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_upstream_urls() -> dict:
    p = _BASE_DIR / "upstream_urls.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_upstream_urls(urls: dict):
    p = _BASE_DIR / "upstream_urls.json"
    p.write_text(json.dumps(urls, ensure_ascii=False, indent=2), encoding="utf-8")


def _user_unlocked_package_ids(db: Session, user_id: int) -> set:
    rows = db.query(SkillUnlock.package_id).filter(SkillUnlock.user_id == user_id).all()
    return {r[0] for r in rows}


@router.get("/skills/store", summary="技能商店列表")
def list_store(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    registry = _load_registry()
    installed = set(_load_installed().get("installed", []))
    unlocked = _user_unlocked_package_ids(db, current_user.id)
    packages = registry.get("packages", {})
    out = []
    for pkg_id, pkg in packages.items():
        is_installed = pkg_id in installed or (pkg.get("unlock_price_yuan") and pkg_id in unlocked)
        out.append({
            "id": pkg_id,
            "name": pkg.get("name", pkg_id),
            "description": pkg.get("description", ""),
            "type": pkg.get("type", ""),
            "tags": pkg.get("tags", []),
            "status": "installed" if is_installed else pkg.get("status", "available"),
            "capabilities_count": len(pkg.get("capabilities", {})),
            "unlock_price_yuan": pkg.get("unlock_price_yuan"),
            "unlocked": pkg_id in unlocked,
        })
    return {"packages": out}


@router.get("/skills/unlocked-packages", summary="当前用户已解锁的技能包 ID 列表（MCP 校验用）")
def unlocked_packages(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ids = list(_user_unlocked_package_ids(db, current_user.id))
    return {"packages": ids}


@router.get("/skills/installed", summary="已安装技能列表")
def list_installed(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    installed_data = _load_installed()
    registry = _load_registry()
    packages = registry.get("packages", {})
    unlocked = _user_unlocked_package_ids(db, current_user.id)
    seen = set()
    out = []
    for pkg_id in installed_data.get("installed", []):
        if pkg_id in seen:
            continue
        seen.add(pkg_id)
        pkg = packages.get(pkg_id, {})
        out.append({
            "id": pkg_id,
            "name": pkg.get("name", pkg_id),
            "description": pkg.get("description", ""),
            "capabilities_count": len(pkg.get("capabilities", {})),
        })
    for pkg_id in unlocked:
        if pkg_id in seen:
            continue
        seen.add(pkg_id)
        pkg = packages.get(pkg_id, {})
        out.append({
            "id": pkg_id,
            "name": pkg.get("name", pkg_id),
            "description": pkg.get("description", ""),
            "capabilities_count": len(pkg.get("capabilities", {})),
        })
    return {"installed": out}


class SkillInstallRequest(BaseModel):
    package_id: str


@router.post("/skills/install", summary="安装技能包")
def install_skill(
    body: SkillInstallRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    registry = _load_registry()
    packages = registry.get("packages", {})
    package = packages.get(body.package_id)
    if not package:
        raise HTTPException(status_code=404, detail=f"技能包 {body.package_id} 不存在")
    if package.get("status") == "coming_soon":
        raise HTTPException(status_code=400, detail="该技能包即将推出，暂不可安装")

    unlock_price = package.get("unlock_price_yuan")
    unlocked = _user_unlocked_package_ids(db, current_user.id)
    if unlock_price and body.package_id not in unlocked:
        return JSONResponse(
            status_code=402,
            content={
                "detail": "该技能需付费解锁",
                "need_payment": True,
                "package_id": body.package_id,
                "package_name": package.get("name", body.package_id),
                "amount_yuan": unlock_price,
            },
        )

    installed_data = _load_installed()
    installed_list = installed_data.get("installed", [])
    if body.package_id in installed_list or (unlock_price and body.package_id in unlocked):
        return {"message": f"{package.get('name', body.package_id)} 已安装", "already_installed": True}

    capabilities = package.get("capabilities", {})
    if capabilities:
        catalog = _load_local_catalog()
        catalog.update(capabilities)
        _save_local_catalog(catalog)
        for cap_id, cap_cfg in capabilities.items():
            existing = db.query(CapabilityConfig).filter(CapabilityConfig.capability_id == cap_id).first()
            if not existing:
                db.add(CapabilityConfig(
                    capability_id=cap_id,
                    description=str(cap_cfg.get("description") or cap_id),
                    upstream=str(cap_cfg.get("upstream") or "sutui"),
                    upstream_tool=str(cap_cfg.get("upstream_tool") or ""),
                    arg_schema=cap_cfg.get("arg_schema") if isinstance(cap_cfg.get("arg_schema"), dict) else None,
                    enabled=True,
                    is_default=bool(cap_cfg.get("is_default", False)),
                    unit_credits=int(cap_cfg.get("unit_credits") or 0),
                ))
        db.commit()

    if not unlock_price and body.package_id not in installed_list:
        installed_list.append(body.package_id)
        installed_data["installed"] = installed_list
        _save_installed(installed_data)

    if package.get("type") == "upstream_mcp":
        config = package.get("config", {})
        upstream_name = config.get("upstream_name", "")
        import os
        upstream_url = os.environ.get(config.get("upstream_url_env", ""), "") or config.get("upstream_url_default", "")
        if upstream_name and upstream_url:
            urls = _load_upstream_urls()
            urls[upstream_name] = upstream_url
            _save_upstream_urls(urls)

    installed_list.append(body.package_id)
    installed_data["installed"] = installed_list
    _save_installed(installed_data)

    return {
        "message": f"已安装 {package.get('name', body.package_id)}，新增 {len(capabilities)} 个能力",
        "package_id": body.package_id,
        "capabilities_added": len(capabilities),
    }


class SkillUnlockOrderCreate(BaseModel):
    package_id: str


class SkillUnlockComplete(BaseModel):
    out_trade_no: Optional[str] = None
    order_id: Optional[int] = None


@router.post("/skills/create-unlock-order", summary="创建技能解锁订单（付费后由管理员 complete 并下发）")
def create_unlock_order(
    body: SkillUnlockOrderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    registry = _load_registry()
    packages = registry.get("packages", {})
    package = packages.get(body.package_id)
    if not package:
        raise HTTPException(status_code=404, detail=f"技能包 {body.package_id} 不存在")
    price = package.get("unlock_price_yuan")
    if not price or price <= 0:
        raise HTTPException(status_code=400, detail="该技能无需付费解锁")
    existing = db.query(SkillUnlock).filter(
        SkillUnlock.user_id == current_user.id,
        SkillUnlock.package_id == body.package_id,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="您已解锁该技能")
    out_trade_no = f"U{current_user.id}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    order = SkillUnlockOrder(
        user_id=current_user.id,
        package_id=body.package_id,
        amount_yuan=price,
        status="pending",
        out_trade_no=out_trade_no,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    hint = getattr(settings, "lobster_recharge_payment_hint", None) or "请通过微信/支付宝转账并联系管理员完成到账，备注订单号。"
    return {
        "order_id": order.id,
        "out_trade_no": order.out_trade_no,
        "package_id": order.package_id,
        "amount_yuan": order.amount_yuan,
        "status": order.status,
        "payment_info": hint,
        "created_at": order.created_at.isoformat() if order.created_at else "",
    }


@router.post("/skills/complete-unlock", summary="完成技能解锁（管理员：到账后写入解锁并下发技能）")
def complete_unlock(
    body: SkillUnlockComplete,
    x_admin_secret: Optional[str] = Header(None, alias="X-Admin-Secret"),
    db: Session = Depends(get_db),
):
    secret = (getattr(settings, "lobster_recharge_admin_secret", None) or "").strip()
    if not secret or (x_admin_secret or "").strip() != secret:
        raise HTTPException(status_code=403, detail="需要管理员密钥")
    if body.out_trade_no:
        order = db.query(SkillUnlockOrder).filter(SkillUnlockOrder.out_trade_no == body.out_trade_no.strip()).first()
    elif body.order_id is not None:
        order = db.query(SkillUnlockOrder).filter(SkillUnlockOrder.id == body.order_id).first()
    else:
        raise HTTPException(status_code=400, detail="请提供 out_trade_no 或 order_id")
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status == "paid":
        return {"ok": True, "message": "订单已处理过", "order_id": order.id}
    unlock = SkillUnlock(user_id=order.user_id, package_id=order.package_id)
    db.add(unlock)
    from datetime import datetime
    order.status = "paid"
    order.paid_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "message": f"已解锁技能 {order.package_id}", "order_id": order.id}


class AddMcpRequest(BaseModel):
    name: str
    url: str


@router.post("/skills/add-mcp", summary="添加 MCP 连接")
def add_mcp(
    body: AddMcpRequest,
    current_user: User = Depends(get_current_user),
):
    name = body.name.strip()
    url = body.url.strip()
    if not name or not url:
        raise HTTPException(status_code=400, detail="名称和 URL 不能为空")

    # 1. Write to openclaw.json
    oc_config_path = _BASE_DIR / "openclaw" / "openclaw.json"
    if oc_config_path.exists():
        try:
            import re
            text = oc_config_path.read_text(encoding="utf-8")
            text = re.sub(r'//.*', '', text)
            config = json.loads(text)
        except Exception:
            config = {}
    else:
        config = {}

    mcp_servers = config.setdefault("mcp", {}).setdefault("servers", {})
    mcp_servers[name] = {"url": url}

    oc_config_path.parent.mkdir(parents=True, exist_ok=True)
    oc_config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # 2. Write to upstream_urls.json
    urls = _load_upstream_urls()
    urls[name] = url
    _save_upstream_urls(urls)

    # 3. Add to skill_registry.json so it shows in the store
    pkg_id = f"mcp_{name}"
    registry = _load_registry()
    packages = registry.setdefault("packages", {})
    if pkg_id not in packages:
        packages[pkg_id] = {
            "name": name,
            "description": f"MCP: {url}",
            "type": "remote_mcp",
            "config": {"mcp_url": url},
            "capabilities": {},
            "tags": ["mcp"],
        }
        p = _BASE_DIR / "skill_registry.json"
        p.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4. Mark as installed
    installed_data = _load_installed()
    installed_list = installed_data.get("installed", [])
    if pkg_id not in installed_list:
        installed_list.append(pkg_id)
        installed_data["installed"] = installed_list
        _save_installed(installed_data)

    return {
        "ok": True,
        "message": f"MCP '{name}' 已添加 ({url})。重启 OpenClaw Gateway 后生效。",
    }


@router.post("/skills/uninstall", summary="卸载技能包")
def uninstall_skill(
    body: SkillInstallRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    registry = _load_registry()
    packages = registry.get("packages", {})
    package = packages.get(body.package_id, {})

    installed_data = _load_installed()
    installed_list = installed_data.get("installed", [])
    if body.package_id not in installed_list:
        raise HTTPException(status_code=400, detail="该技能包未安装")

    capabilities = package.get("capabilities", {})
    if capabilities:
        catalog = _load_local_catalog()
        for cap_id in capabilities:
            catalog.pop(cap_id, None)
            existing = db.query(CapabilityConfig).filter(CapabilityConfig.capability_id == cap_id).first()
            if existing:
                db.delete(existing)
        _save_local_catalog(catalog)
        db.commit()

    installed_list.remove(body.package_id)
    installed_data["installed"] = installed_list
    _save_installed(installed_data)

    return {"message": f"已卸载 {package.get('name', body.package_id)}", "package_id": body.package_id}
