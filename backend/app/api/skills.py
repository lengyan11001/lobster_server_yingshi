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
from ..services.credit_ledger import append_credit_ledger
from ..services.credits_amount import quantize_credits, user_balance_decimal
from .installation_slots import ensure_installation_slot, installation_slots_enabled, parse_installation_id_strict

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent


def _skill_store_admin(user: User) -> bool:
    """技能商店：管理员可见「调试中」包；role=admin 或账号 test01。"""
    if (getattr(user, "role", None) or "").strip() == "admin":
        return True
    email = (getattr(user, "email", None) or "").strip().lower()
    return email == "test01"


def _pkg_store_visibility(pkg: dict) -> str:
    """未声明时视为 debug（仅管理员在商店可见）。"""
    v = (pkg.get("store_visibility") or "").strip().lower()
    if v in ("online", "debug"):
        return v
    return "debug"


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


def _capability_to_package_map() -> dict:
    """capability_id -> package_id。含 unlock_price_yuan 或 unlock_price_credits 的技能包下的能力，均须已解锁。"""
    registry = _load_registry()
    out = {}
    for pkg_id, pkg in registry.get("packages", {}).items():
        yuan = int(pkg.get("unlock_price_yuan") or 0)
        cred = int(pkg.get("unlock_price_credits") or 0)
        if yuan <= 0 and cred <= 0:
            continue
        for cap_id in pkg.get("capabilities", {}).keys():
            out[cap_id] = pkg_id
    return out


def user_can_use_capability(
    db: Session,
    user_id: int,
    capability_id: str,
    installation_id: Optional[str] = None,
) -> bool:
    """该用户是否可使用此能力：若能力属于需付费解锁的技能包，则必须已解锁；在线版还需登记当前 installation_id（由路由先 parse_installation_id_strict）。"""
    cap_map = _capability_to_package_map()
    package_id = cap_map.get(capability_id)
    if not package_id:
        return True  # 不属付费包，可用
    unlocked = _user_unlocked_package_ids(db, user_id)
    if package_id not in unlocked:
        return False
    if not installation_slots_enabled():
        return True
    iid = (installation_id or "").strip()
    if not iid:
        return False
    ensure_installation_slot(db, user_id, iid)
    return True


def _package_capabilities_already_in_catalog(db: Session, pkg: dict) -> bool:
    """该包声明的能力是否已全部存在于 CapabilityConfig 且 enabled。用于与「能力可用」状态对齐，避免卡片显示未安装但能力能用。"""
    cap_ids = list((pkg.get("capabilities") or {}).keys())
    if not cap_ids:
        return False
    n_enabled = db.query(CapabilityConfig).filter(
        CapabilityConfig.capability_id.in_(cap_ids),
        CapabilityConfig.enabled.is_(True),
    ).count()
    return n_enabled == len(cap_ids)


@router.get("/skills/store", summary="技能商店列表")
def list_store(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    registry = _load_registry()
    installed = set(_load_installed().get("installed", []))
    unlocked = _user_unlocked_package_ids(db, current_user.id)
    packages = registry.get("packages", {})
    is_admin = _skill_store_admin(current_user)
    out = []
    for pkg_id, pkg in packages.items():
        if pkg.get("show_in_store") is False:
            continue
        if _pkg_store_visibility(pkg) == "debug" and not is_admin:
            continue
        is_installed = (
            pkg_id in installed
            or (pkg.get("unlock_price_yuan") and pkg_id in unlocked)
            or (pkg.get("unlock_price_credits") and pkg_id in unlocked)
            or pkg.get("default_installed")
            or _package_capabilities_already_in_catalog(db, pkg)
        )
        out.append({
            "id": pkg_id,
            "name": pkg.get("name", pkg_id),
            "description": pkg.get("description", ""),
            "type": pkg.get("type", ""),
            "tags": pkg.get("tags", []),
            "store_visibility": _pkg_store_visibility(pkg),
            "status": "installed" if is_installed else pkg.get("status", "available"),
            "capabilities_count": len(pkg.get("capabilities", {})),
            "unlock_price_yuan": pkg.get("unlock_price_yuan"),
            "unlock_price_credits": pkg.get("unlock_price_credits"),
            "default_installed": pkg.get("default_installed"),
            "unlocked": pkg_id in unlocked,
        })
    return {"packages": out, "is_skill_store_admin": is_admin}


@router.get("/skills/skill-store-admin", summary="当前用户是否为技能商店管理员（可见调试包与调试能力）")
def skill_store_admin_flag(current_user: User = Depends(get_current_user)):
    return {"is_skill_store_admin": _skill_store_admin(current_user)}


@router.get("/skills/unlocked-packages", summary="当前用户已解锁的技能包 ID 列表（MCP 校验用）")
def unlocked_packages(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ids = list(_user_unlocked_package_ids(db, current_user.id))
    return {"packages": ids}


ADD_MORE_PUBLISH_ACCOUNTS_PACKAGE_ID = "add_more_publish_accounts"


def _user_has_add_more_publish_accounts_unlock(db: Session, user_id: int) -> bool:
    return (
        db.query(SkillUnlock).filter(
            SkillUnlock.user_id == user_id,
            SkillUnlock.package_id == ADD_MORE_PUBLISH_ACCOUNTS_PACKAGE_ID,
        ).first()
        is not None
    )


@router.get("/skills/publish-add-account-eligible", summary="发布账号：是否允许再添加（仅服务端判定）")
def publish_add_account_eligible(
    existing_local_count: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    x_installation_id: Optional[str] = Header(None, alias="X-Installation-Id"),
):
    """浏览器每次点击「添加账号」前先请求本接口（走公网 API_BASE）。existing_local_count 为当前本机已有账号数，由前端传入。"""
    if existing_local_count < 1:
        return {"allowed": True}
    if _user_has_add_more_publish_accounts_unlock(db, current_user.id):
        if installation_slots_enabled():
            iid = parse_installation_id_strict(x_installation_id)
            ensure_installation_slot(db, current_user.id, iid)
        return {"allowed": True}
    reg = _load_registry()
    pkg = (reg.get("packages") or {}).get(ADD_MORE_PUBLISH_ACCOUNTS_PACKAGE_ID) or {}
    amount = int(pkg.get("unlock_price_credits") or 0)
    if amount <= 0:
        if installation_slots_enabled():
            iid = parse_installation_id_strict(x_installation_id)
            ensure_installation_slot(db, current_user.id, iid)
        return {"allowed": True}
    return {
        "allowed": False,
        "need_credits_unlock": True,
        "package_id": ADD_MORE_PUBLISH_ACCOUNTS_PACKAGE_ID,
        "amount_credits": amount,
        "detail": "当前仅可添加 1 个发布账号。解锁「添加更多发布账号」后可添加多个。",
    }


WECOM_REPLY_PACKAGE_ID = "wecom_reply"


def _user_has_wecom_reply_unlock(db: Session, user_id: int) -> bool:
    return (
        db.query(SkillUnlock).filter(
            SkillUnlock.user_id == user_id,
            SkillUnlock.package_id == WECOM_REPLY_PACKAGE_ID,
        ).first()
        is not None
    )


@router.get("/skills/wecom-config-eligible", summary="企业微信配置：是否已解锁（仅服务端判定）")
def wecom_config_eligible(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    x_installation_id: Optional[str] = Header(None, alias="X-Installation-Id"),
):
    """企微配置存在本机；点击卡片或进入配置页前由浏览器调本接口（走 API_BASE）。"""
    if _user_has_wecom_reply_unlock(db, current_user.id):
        if installation_slots_enabled():
            iid = parse_installation_id_strict(x_installation_id)
            ensure_installation_slot(db, current_user.id, iid)
        return {"allowed": True}
    reg = _load_registry()
    pkg = (reg.get("packages") or {}).get(WECOM_REPLY_PACKAGE_ID) or {}
    amount = int(pkg.get("unlock_price_credits") or 1000)
    return {
        "allowed": False,
        "need_credits_unlock": True,
        "package_id": WECOM_REPLY_PACKAGE_ID,
        "amount_credits": amount,
        "detail": "请使用积分解锁「企业微信自动回复」后再管理本地配置。",
    }


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
    if _pkg_store_visibility(package) == "debug" and not _skill_store_admin(current_user):
        raise HTTPException(status_code=403, detail="该技能包为调试中，仅管理员可安装")

    unlock_price = package.get("unlock_price_yuan")
    unlock_credits = package.get("unlock_price_credits")
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
    if unlock_credits and body.package_id not in unlocked:
        need = int(unlock_credits)
        return JSONResponse(
            status_code=402,
            content={
                "detail": f"该技能需 {need} 积分解锁，请先调用「积分解锁」或充值后再安装。",
                "need_credits_unlock": True,
                "package_id": body.package_id,
                "package_name": package.get("name", body.package_id),
                "amount_credits": need,
            },
        )

    installed_data = _load_installed()
    installed_list = installed_data.get("installed", [])
    if (
        body.package_id in installed_list
        or (unlock_price and body.package_id in unlocked)
        or (unlock_credits and body.package_id in unlocked)
        or package.get("default_installed")
    ):
        return {"message": f"{package.get('name', body.package_id)} 已安装", "already_installed": True, "capabilities_added": 0}

    # 该包能力已全部在 CapabilityConfig 中（如预置或别包装入）→ 视为已安装并同步写入 installed，避免「卡片未安装但能力能用」
    capabilities = package.get("capabilities", {})
    if capabilities and _package_capabilities_already_in_catalog(db, package):
        if body.package_id not in installed_list:
            installed_list.append(body.package_id)
            installed_data["installed"] = installed_list
            _save_installed(installed_data)
        return {
            "message": f"{package.get('name', body.package_id)} 已安装（能力已在系统中）",
            "already_installed": True,
            "capabilities_added": 0,
        }

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


class UnlockByCreditsRequest(BaseModel):
    package_id: str


@router.post("/skills/unlock-by-credits", summary="使用积分解锁技能（如抖音/小红书/企微 1000 积分）")
def unlock_by_credits(
    body: UnlockByCreditsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    x_installation_id: Optional[str] = Header(None, alias="X-Installation-Id"),
):
    registry = _load_registry()
    packages = registry.get("packages", {})
    package = packages.get(body.package_id)
    if not package:
        raise HTTPException(status_code=404, detail=f"技能包 {body.package_id} 不存在")
    if _pkg_store_visibility(package) == "debug" and not _skill_store_admin(current_user):
        raise HTTPException(status_code=403, detail="该技能包为调试中，仅管理员可积分解锁")
    need = package.get("unlock_price_credits")
    if not need or int(need) <= 0:
        raise HTTPException(status_code=400, detail="该技能不支持积分解锁")
    need = int(need)
    need_d = quantize_credits(need)
    unlocked = _user_unlocked_package_ids(db, current_user.id)
    if body.package_id in unlocked:
        if installation_slots_enabled():
            iid = parse_installation_id_strict(x_installation_id)
            ensure_installation_slot(db, current_user.id, iid)
        return {"ok": True, "message": f"已解锁 {package.get('name', body.package_id)}", "already_unlocked": True}
    db.refresh(current_user)
    if user_balance_decimal(current_user) < need_d:
        raise HTTPException(
            status_code=402,
            detail=f"积分不足：解锁需 {need} 积分，当前余额 {user_balance_decimal(current_user)}。请先充值。",
        )
    current_user.credits = user_balance_decimal(current_user) - need_d
    bal = quantize_credits(current_user.credits)
    append_credit_ledger(
        db,
        current_user.id,
        -need_d,
        "skill_unlock",
        bal,
        description=f"积分解锁技能 {package.get('name', body.package_id)}",
        ref_type="skill_package",
        ref_id=(body.package_id or "")[:128],
        meta={"package_id": body.package_id, "need_credits": need},
    )
    db.add(SkillUnlock(user_id=current_user.id, package_id=body.package_id))
    if installation_slots_enabled():
        iid = parse_installation_id_strict(x_installation_id)
        ensure_installation_slot(db, current_user.id, iid)
    db.commit()
    return {"ok": True, "message": f"已用 {need} 积分解锁 {package.get('name', body.package_id)}", "credits_deducted": need}


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
    if _pkg_store_visibility(package) == "debug" and not _skill_store_admin(current_user):
        raise HTTPException(status_code=403, detail="该技能包为调试中，仅管理员可创建付费解锁订单")
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
            "store_visibility": "debug",
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
