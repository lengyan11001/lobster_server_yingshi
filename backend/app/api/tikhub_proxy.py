"""TikHub 多平台数据代理：服务端持有统一 Token，按白名单转发到 https://api.tikhub.io，按积分计费。

设计要点（参考 comfly_proxy.py）：
- 客户端永远不持有 TikHub Token，所有请求都打本路由
- 仅放行 catalog.json (skills/tikhub_explorer/catalog.json) 中已登记的 endpoint_id
- 计费：调用前预扣（按 tikhub_pricing.json 单价），HTTP 失败全额退款，HTTP 200 但响应 code 非 0/200 也退款
- 写 capability_call_logs，capability_id="tikhub.fetch"
- 简单 per-user 速率限制：60 req/min
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
from collections import deque
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from ..models import CapabilityCallLog, User
from ..services.credit_ledger import append_credit_ledger
from ..services.credits_amount import (
    credits_json_float,
    quantize_credits,
    user_balance_decimal,
)
from .auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SKILL_DIR = _REPO_ROOT / "skills" / "tikhub_explorer"
_CATALOG_PATH = _SKILL_DIR / "catalog.json"
_PRICING_PATH = _REPO_ROOT / "tikhub_pricing.json"

_CAPABILITY_ID = "tikhub.fetch"
_HTTP_TIMEOUT = 30.0
_RATE_LIMIT_PER_MIN = 60

# ---------------------------------------------------------------------------
# Globals: catalog cache + rate limiter
# ---------------------------------------------------------------------------
_catalog_cache: Optional[Dict[str, Any]] = None
_catalog_mtime: float = 0.0
_catalog_lock = Lock()

_pricing_cache: Optional[Dict[str, Any]] = None
_pricing_mtime: float = 0.0

_user_call_history: Dict[int, deque] = {}
_rate_lock = Lock()

_pool_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _pool_client
    if _pool_client is None or _pool_client.is_closed:
        _pool_client = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=10, keepalive_expiry=120),
        )
    return _pool_client


def _load_catalog(force: bool = False) -> Dict[str, Any]:
    global _catalog_cache, _catalog_mtime
    with _catalog_lock:
        if not _CATALOG_PATH.exists():
            return {"platforms": [], "endpoints_index": {}, "endpoint_count": 0, "platform_count": 0}
        mtime = _CATALOG_PATH.stat().st_mtime
        if force or _catalog_cache is None or mtime != _catalog_mtime:
            try:
                _catalog_cache = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
                _catalog_mtime = mtime
            except Exception as e:
                logger.exception("[tikhub_proxy] failed to load catalog: %s", e)
                _catalog_cache = {"platforms": [], "endpoints_index": {}, "endpoint_count": 0, "platform_count": 0}
        return _catalog_cache or {}


def _load_pricing() -> Dict[str, Any]:
    global _pricing_cache, _pricing_mtime
    if not _PRICING_PATH.exists():
        return {"default_unit_credits": 1, "platforms": {}, "overrides": {}}
    mtime = _PRICING_PATH.stat().st_mtime
    if _pricing_cache is None or mtime != _pricing_mtime:
        try:
            _pricing_cache = json.loads(_PRICING_PATH.read_text(encoding="utf-8"))
            _pricing_mtime = mtime
        except Exception:
            _pricing_cache = {"default_unit_credits": 1, "platforms": {}, "overrides": {}}
    return _pricing_cache or {}


def _unit_credits_for(endpoint_id: str, platform: str) -> int:
    pricing = _load_pricing()
    overrides = pricing.get("overrides") or {}
    if endpoint_id in overrides:
        try:
            return max(0, int(overrides[endpoint_id].get("unit_credits", 1)))
        except Exception:
            pass
    plat_table = pricing.get("platforms") or {}
    if platform in plat_table:
        try:
            return max(0, int(plat_table[platform].get("unit_credits", 1)))
        except Exception:
            pass
    try:
        return max(0, int(pricing.get("default_unit_credits", 1)))
    except Exception:
        return 1


def _slim_catalog(catalog: Dict[str, Any]) -> Dict[str, Any]:
    """前端默认拿到的瘦身版（去掉 endpoints_index、endpoint.summary、param.description）。"""
    out_platforms: List[Dict[str, Any]] = []
    for p in catalog.get("platforms", []):
        groups_out = []
        for g in p.get("groups", []):
            eps = []
            for e in g.get("endpoints", []):
                eps.append({
                    "id": e.get("id"),
                    "title": e.get("title"),
                    "method": e.get("method"),
                    "params": [
                        {k: v for k, v in prm.items() if k != "description"}
                        for prm in (e.get("params") or [])
                    ],
                    "pagination": e.get("pagination"),
                })
            groups_out.append({"id": g.get("id"), "name": g.get("name"), "endpoints": eps})
        out_platforms.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "icon": p.get("icon"),
            "groups": groups_out,
        })
    return {
        "version": catalog.get("version", "1.0"),
        "generated_at": catalog.get("generated_at"),
        "platform_count": catalog.get("platform_count"),
        "endpoint_count": catalog.get("endpoint_count"),
        "platforms": out_platforms,
    }


def _check_rate_limit(user_id: int) -> None:
    now = time.time()
    with _rate_lock:
        dq = _user_call_history.setdefault(user_id, deque())
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT_PER_MIN:
            wait = int(60 - (now - dq[0]))
            raise HTTPException(429, f"调用太频繁：每用户每分钟最多 {_RATE_LIMIT_PER_MIN} 次，请 {wait}s 后重试。")
        dq.append(now)


# ---------------------------------------------------------------------------
# Billing helpers (parallel of comfly_proxy)
# ---------------------------------------------------------------------------

def _should_deduct() -> bool:
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    return edition == "online" and getattr(settings, "lobster_independent_auth", True)


def _do_pre_deduct(db: Session, user: User, credits: int, *, endpoint_id: str) -> Decimal:
    if not _should_deduct() or credits <= 0:
        return Decimal("0")
    fc = quantize_credits(credits)
    db.refresh(user)
    if user_balance_decimal(user) < fc:
        raise HTTPException(
            status_code=402,
            detail=f"积分不足：本次需 {float(fc)}，当前余额 {float(user_balance_decimal(user))}。",
        )
    user.credits = user_balance_decimal(user) - fc
    bal = quantize_credits(user.credits)
    append_credit_ledger(
        db, user.id, -fc, "pre_deduct", bal,
        description=f"TikHub proxy 预扣 ({endpoint_id})",
        ref_type="tikhub_proxy",
        meta={"capability_id": _CAPABILITY_ID, "endpoint_id": endpoint_id, "upstream": "tikhub"},
    )
    db.commit()
    return fc


def _do_full_refund(db: Session, user: User, *, pre: Decimal, endpoint_id: str, error: str = "") -> None:
    if not _should_deduct() or pre <= 0:
        return
    db.refresh(user)
    user.credits = user_balance_decimal(user) + pre
    bal = quantize_credits(user.credits)
    append_credit_ledger(
        db, user.id, pre, "refund", bal,
        description=f"TikHub proxy 调用失败全额退款 ({endpoint_id})",
        ref_type="tikhub_proxy",
        meta={
            "capability_id": _CAPABILITY_ID,
            "endpoint_id": endpoint_id,
            "refunded": credits_json_float(pre),
            "upstream": "tikhub",
            "error": (error or "")[:500],
        },
    )
    db.commit()


def _log_call(
    db: Session,
    user: User,
    *,
    endpoint_id: str,
    success: bool,
    credits_charged: int,
    latency_ms: int,
    request_payload: Dict[str, Any],
    response_payload: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    status: Optional[str] = None,
) -> None:
    try:
        row = CapabilityCallLog(
            user_id=user.id,
            capability_id=_CAPABILITY_ID,
            upstream="tikhub",
            upstream_tool=endpoint_id,
            success=bool(success),
            credits_charged=quantize_credits(max(0, int(credits_charged))),
            latency_ms=latency_ms,
            request_payload=request_payload,
            response_payload=response_payload,
            error_message=error_message,
            source="proxy",
            status=status,
        )
        db.add(row)
        db.commit()
    except Exception as e:
        logger.warning("[tikhub_proxy] log_call failed: %s", e)
        db.rollback()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CallBody(BaseModel):
    endpoint_id: str
    params: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@router.get("/api/tikhub-proxy/catalog", summary="TikHub 接口目录（瘦身版，前端用）")
def get_catalog(
    detail: int = Query(0, description="1=返回完整 catalog（含 summary 与 endpoints_index），仅管理员/调试用"),
    current_user: User = Depends(get_current_user),
):
    catalog = _load_catalog()
    if detail:
        return catalog
    return _slim_catalog(catalog)


@router.get("/api/tikhub-proxy/endpoint/{endpoint_id}", summary="单个接口详情（含 summary 与原始描述）")
def get_endpoint(endpoint_id: str, current_user: User = Depends(get_current_user)):
    catalog = _load_catalog()
    idx = (catalog.get("endpoints_index") or {}).get(endpoint_id)
    if not idx:
        raise HTTPException(404, f"未知 endpoint_id: {endpoint_id}")
    pricing = _unit_credits_for(endpoint_id, idx.get("platform", ""))
    return {**idx, "unit_credits": pricing}


@router.get("/api/tikhub-proxy/balance", summary="当前积分余额（前端展示用）")
def get_balance(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.refresh(current_user)
    bal = user_balance_decimal(current_user)
    return {"credits": credits_json_float(bal)}


@router.post("/api/tikhub-proxy/call", summary="代理调用 TikHub")
async def call_tikhub(
    body: CallBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    catalog = _load_catalog()
    idx = (catalog.get("endpoints_index") or {}).get(body.endpoint_id)
    if not idx:
        raise HTTPException(400, f"未注册的 endpoint_id: {body.endpoint_id}")

    api_base = (settings.tikhub_api_base or "https://api.tikhub.io").rstrip("/")
    api_key = (settings.tikhub_api_key or "").strip()
    if not api_key:
        raise HTTPException(503, "服务端未配置 TIKHUB_API_KEY，请联系管理员")

    _check_rate_limit(current_user.id)

    # 路径参数替换 + 拆 query
    path = idx["path"]
    method = (idx.get("method") or "GET").upper()
    query_params: Dict[str, Any] = {}
    used_path_keys = set()
    for prm in idx.get("params") or []:
        name = prm["name"]
        loc = prm.get("in", "query")
        val = body.params.get(name)
        if val is None or val == "":
            if prm.get("required") and loc == "path":
                raise HTTPException(400, f"缺少必填参数: {name}")
            continue
        if loc == "path":
            path = path.replace("{" + name + "}", str(val))
            used_path_keys.add(name)
        else:
            query_params[name] = val

    # 任何 catalog 内未声明的参数全部拒绝（防注入）
    allowed_names = {prm["name"] for prm in idx.get("params") or []}
    extra = set(body.params.keys()) - allowed_names
    if extra:
        raise HTTPException(400, f"未识别的参数: {sorted(extra)}")

    # 计费
    unit = _unit_credits_for(body.endpoint_id, idx.get("platform", ""))
    pre = _do_pre_deduct(db, current_user, unit, endpoint_id=body.endpoint_id)

    url = api_base + path
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "lobster-tikhub-proxy/1.0",
    }
    started = time.time()
    success = False
    status_code = 0
    upstream_data: Optional[Dict[str, Any]] = None
    error_msg: Optional[str] = None
    try:
        client = _get_client()
        if method == "GET":
            r = await client.get(url, params=query_params, headers=headers)
        else:
            r = await client.post(url, params=query_params, headers=headers, json=body.params or {})
        status_code = r.status_code
        try:
            upstream_data = r.json() if r.content else {}
        except Exception:
            upstream_data = {"_raw_text": (r.text or "")[:1000]}

        # 业务层判断：HTTP 200 但 code 不在 [0,200] 也算失败（TikHub 统一 ResponseModel）
        if status_code >= 400:
            error_msg = f"TikHub HTTP {status_code}: {(r.text or '')[:300]}"
        else:
            biz_code = (upstream_data or {}).get("code")
            if biz_code is not None and int(biz_code) not in (0, 200):
                error_msg = f"TikHub 业务失败 code={biz_code}: {(upstream_data or {}).get('message', '')[:300]}"
            else:
                success = True
    except Exception as e:
        error_msg = f"网络/上游异常: {e}"

    latency_ms = int((time.time() - started) * 1000)

    if not success:
        _do_full_refund(db, current_user, pre=pre, endpoint_id=body.endpoint_id, error=error_msg or "")
        _log_call(
            db, current_user, endpoint_id=body.endpoint_id, success=False,
            credits_charged=0, latency_ms=latency_ms,
            request_payload={"endpoint_id": body.endpoint_id, "params": body.params, "url": url},
            response_payload={"status_code": status_code, "data": _trim_for_log(upstream_data)},
            error_message=error_msg, status="failed",
        )
        raise HTTPException(status_code=502 if status_code == 0 else status_code,
                            detail=error_msg or "TikHub 调用失败")

    _log_call(
        db, current_user, endpoint_id=body.endpoint_id, success=True,
        credits_charged=int(pre), latency_ms=latency_ms,
        request_payload={"endpoint_id": body.endpoint_id, "params": body.params},
        response_payload={"status_code": status_code, "summary": _summarize(upstream_data)},
        status="ok",
    )
    return {
        "ok": True,
        "endpoint_id": body.endpoint_id,
        "unit_credits": unit,
        "data": upstream_data,
        "latency_ms": latency_ms,
    }


def _trim_for_log(data: Any, max_len: int = 2000) -> Any:
    try:
        s = json.dumps(data, ensure_ascii=False)
    except Exception:
        return {"_unloggable": True}
    if len(s) <= max_len:
        return data
    return {"_truncated": True, "preview": s[:max_len]}


def _summarize(data: Any) -> Dict[str, Any]:
    """只摘出顶层 keys + list 长度，避免把视频 url 等大对象塞进日志表。"""
    if not isinstance(data, dict):
        return {"_type": type(data).__name__}
    out: Dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, list):
            out[k] = f"<list len={len(v)}>"
        elif isinstance(v, dict):
            out[k] = f"<dict keys={list(v.keys())[:8]}>"
        else:
            out[k] = v if (isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) < 80)) else f"<{type(v).__name__}>"
    return out


# ---------------------------------------------------------------------------
# Catalog refresh (admin only): exposed via admin.py wrapper
# ---------------------------------------------------------------------------

def refresh_catalog_blocking(force: bool = True) -> Dict[str, Any]:
    """同步运行 build_catalog.py，重新拉 OpenAPI 并写 catalog.json。返回新 catalog meta。"""
    script = _SKILL_DIR / "build_catalog.py"
    if not script.exists():
        raise RuntimeError(f"build_catalog.py 不存在: {script}")
    env = {**__import__("os").environ}
    if settings.tikhub_api_key:
        env["TIKHUB_API_KEY"] = settings.tikhub_api_key
    args = [sys.executable, str(script)]
    if force:
        args.append("--refresh")
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=120, env=env, encoding="utf-8")
    except subprocess.TimeoutExpired:
        raise RuntimeError("build_catalog 超时")
    if proc.returncode != 0:
        raise RuntimeError(f"build_catalog 失败: {proc.stderr[-400:] or proc.stdout[-400:]}")
    catalog = _load_catalog(force=True)
    return {
        "platform_count": catalog.get("platform_count"),
        "endpoint_count": catalog.get("endpoint_count"),
        "generated_at": catalog.get("generated_at"),
        "stdout_tail": proc.stdout[-400:],
    }
