"""Comfly 透明 Proxy：让用户客户端 (lobster_online) 内的爆款TVC pipeline 走云端 Comfly Token + 龙虾积分计费。

为什么需要：
- 爆款TVC pipeline (skills/comfly_veo3_daihuo_video) 内部会调 Comfly 的 4 个端点：
  POST /v1/chat/completions       (分镜规划，按 token usage 计费)
  POST /v1/images/generations     (分镜图，按 per_call 计费)
  POST /v2/videos/generations     (Veo 视频提交，按 per_call 计费)
  GET  /v2/videos/generations/{id}(Veo 任务轮询，不计费)
- 之前每个用户必须自己在「技能商店」配 Comfly API Key，按 Comfly 账户余额扣费。
- 现在改成统一走云端 server token (env: COMFLY_API_KEY[_<GROUP>])，按 comfly_pricing.json 扣龙虾积分。

设计：
- 透明转发：proxy 不重新组装 body，直接把客户端构造好的 body POST 给 Comfly，只替换 Authorization。
- 计费：① 调用前按估算预扣 → ② 调 Comfly → ③ chat 按 usage 结算差额；image/video 按 per_call 实扣（估算==实际）；失败全额退款。
- 鉴权：用户 JWT。
- token_group：按 model 在 comfly_pricing.json 配置的 token_group 选用对应 env 的 Key。
"""
from __future__ import annotations

import json
import logging
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..services.credit_ledger import append_credit_ledger
from ..services.credits_amount import quantize_credits, credits_json_float, user_balance_decimal
from .auth import get_current_user

# 让本模块能 import mcp/ 下的 comfly_upstream
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.comfly_upstream import (  # noqa: E402
    estimate_comfly_credits,
    get_comfly_config,
    lookup_comfly_model,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_PROXY_AUDIT_LOGGER = logging.getLogger("comfly_proxy_audit")

# Comfly 上游超时（与 pipeline 默认 poll 间隔对齐，video submit 通常很快返回 task_id）
_TIMEOUT_CHAT = 120.0
_TIMEOUT_IMAGE = 180.0
_TIMEOUT_VIDEO_SUBMIT = 60.0
_TIMEOUT_VIDEO_POLL = 30.0


def _should_deduct_credits() -> bool:
    """与 capabilities.py / sutui_chat_proxy.py 一致：在线版独立认证才扣积分。"""
    from ..core.config import settings
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    return edition == "online" and getattr(settings, "lobster_independent_auth", True)


def _model_token_group(model_id: str) -> str:
    entry = lookup_comfly_model(model_id) or {}
    return (entry.get("token_group") or "").strip()


def _audit(event: str, **kw: Any) -> None:
    """JSONL 审计日志（与 sutui_audit 同 logger 风格）。"""
    try:
        payload = {"event": event, **kw}
        _PROXY_AUDIT_LOGGER.info("[comfly_proxy_audit] %s", json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        pass


def _check_request_authorized_for_billing(request: Request) -> None:
    """与 /capabilities/pre-deduct 同口径：非本机回环且无 X-Lobster-Mcp-Billing 时拒绝，避免被外部直接打。

    爆款TVC proxy 是用户客户端发来的，只要带有效 JWT 即可，不强制 billing key（与 sutui_chat_proxy 一致）。
    本函数预留扩展点：如未来要求强制 billing key，把判断打开即可。
    """
    return None


def _do_pre_deduct(
    db: Session, user: User, credits: int, *,
    capability_id: str, model: str, endpoint: str, extra_meta: Optional[Dict[str, Any]] = None,
) -> Decimal:
    """直接扣账（与 capabilities.py force_credits 路径一致）。返回实际扣的 Decimal。"""
    if not _should_deduct_credits() or credits <= 0:
        return Decimal("0")
    fc = quantize_credits(credits)
    db.refresh(user)
    if user_balance_decimal(user) < fc:
        raise HTTPException(
            status_code=402,
            detail=f"积分不足：本次预扣 {float(fc)}，当前余额 {float(user_balance_decimal(user))}。",
        )
    user.credits = user_balance_decimal(user) - fc
    bal = quantize_credits(user.credits)
    append_credit_ledger(
        db, user.id, -fc, "pre_deduct", bal,
        description=f"Comfly proxy 预扣 ({endpoint})",
        ref_type="comfly_proxy",
        meta={
            "capability_id": capability_id, "model": model, "endpoint": endpoint,
            "pre_estimated": credits_json_float(fc), "upstream": "comfly",
            **(extra_meta or {}),
        },
    )
    db.commit()
    return fc


def _do_settle(
    db: Session, user: User, *, pre: Decimal, actual: int,
    capability_id: str, model: str, endpoint: str, extra_meta: Optional[Dict[str, Any]] = None,
) -> None:
    """实际 vs 预扣的差额结算。actual<pre 退差额，actual>pre 再扣差额。"""
    if not _should_deduct_credits():
        return
    actual_dec = quantize_credits(max(0, int(actual)))
    delta = actual_dec - pre  # >0 需补扣，<0 需退款
    if delta == 0:
        return
    db.refresh(user)
    if delta > 0:
        # 补扣：余额不足时不阻断（已经走完上游），只能记账让管理员对账
        cur_bal = user_balance_decimal(user)
        deduct_now = min(cur_bal, delta) if cur_bal > 0 else Decimal("0")
        user.credits = cur_bal - deduct_now
        bal = quantize_credits(user.credits)
        append_credit_ledger(
            db, user.id, -deduct_now, "settle", bal,
            description=f"Comfly proxy 结算补扣 ({endpoint}) actual={actual} pre={float(pre)}",
            ref_type="comfly_proxy",
            meta={
                "capability_id": capability_id, "model": model, "endpoint": endpoint,
                "pre_estimated": credits_json_float(pre), "actual": credits_json_float(actual_dec),
                "delta": credits_json_float(delta), "upstream": "comfly",
                **(extra_meta or {}),
            },
        )
        if deduct_now < delta:
            logger.warning(
                "[comfly_proxy] 用户 %s 结算补扣不足额：需 %s，仅扣 %s（余额耗尽）",
                user.id, float(delta), float(deduct_now),
            )
    else:
        # 退款
        refund_amt = -delta
        user.credits = user_balance_decimal(user) + refund_amt
        bal = quantize_credits(user.credits)
        append_credit_ledger(
            db, user.id, refund_amt, "refund", bal,
            description=f"Comfly proxy 结算退款 ({endpoint}) actual={actual} pre={float(pre)}",
            ref_type="comfly_proxy",
            meta={
                "capability_id": capability_id, "model": model, "endpoint": endpoint,
                "pre_estimated": credits_json_float(pre), "actual": credits_json_float(actual_dec),
                "delta": credits_json_float(delta), "upstream": "comfly",
                **(extra_meta or {}),
            },
        )
    db.commit()


def _do_full_refund(
    db: Session, user: User, *, pre: Decimal,
    capability_id: str, model: str, endpoint: str, error: str = "",
) -> None:
    if not _should_deduct_credits() or pre <= 0:
        return
    db.refresh(user)
    user.credits = user_balance_decimal(user) + pre
    bal = quantize_credits(user.credits)
    append_credit_ledger(
        db, user.id, pre, "refund", bal,
        description=f"Comfly proxy 调用失败全额退款 ({endpoint})",
        ref_type="comfly_proxy",
        meta={
            "capability_id": capability_id, "model": model, "endpoint": endpoint,
            "refunded": credits_json_float(pre), "upstream": "comfly",
            "error": (error or "")[:500],
        },
    )
    db.commit()


async def _comfly_request(
    method: str, url: str, body: Optional[Dict[str, Any]], headers: Dict[str, str], timeout: float,
) -> Dict[str, Any]:
    """统一封装 httpx 调用 Comfly。失败抛 RuntimeError，含状态码与文本片段。"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        if method.upper() == "GET":
            r = await client.get(url, headers=headers)
        else:
            r = await client.post(url, headers=headers, json=body or {})
    if r.status_code >= 400:
        raise RuntimeError(f"Comfly HTTP {r.status_code}: {(r.text or '')[:500]}")
    try:
        return r.json() if r.content else {}
    except Exception:
        return {"_raw_text": r.text}


def _comfly_url(path: str, model: str = "") -> str:
    base, _ = get_comfly_config(_model_token_group(model))
    if not base:
        raise HTTPException(503, "服务端未配置 Comfly：缺少环境变量 COMFLY_API_BASE")
    return base.rstrip("/") + path


def _comfly_headers(model: str = "") -> Dict[str, str]:
    _, key = get_comfly_config(_model_token_group(model))
    if not key:
        raise HTTPException(503, "服务端未配置 Comfly Key：缺少环境变量 COMFLY_API_KEY")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_CAPABILITY_FOR_BILLING = "comfly.veo.daihuo_pipeline"


@router.post("/api/comfly-proxy/v1/chat/completions", summary="Comfly chat 透明 proxy（按 token usage 计费）")
async def proxy_chat_completions(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_request_authorized_for_billing(request)
    body = await request.json()
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "缺少 model")
    if not lookup_comfly_model(model):
        raise HTTPException(400, f"模型 {model} 未在 comfly_pricing.json 注册，无法计费")

    # 预扣（按典型 token 估算）
    estimated = estimate_comfly_credits(model, {}, for_user=True) or 1
    pre = _do_pre_deduct(db, current_user, estimated,
                         capability_id=_CAPABILITY_FOR_BILLING, model=model, endpoint="chat")
    _audit("chat_pre_deduct", user_id=current_user.id, model=model, estimated=estimated)

    try:
        resp = await _comfly_request("POST", _comfly_url("/v1/chat/completions", model),
                                     body, _comfly_headers(model), _TIMEOUT_CHAT)
    except Exception as e:
        _do_full_refund(db, current_user, pre=pre,
                        capability_id=_CAPABILITY_FOR_BILLING, model=model, endpoint="chat", error=str(e))
        _audit("chat_failed", user_id=current_user.id, model=model, error=str(e)[:300])
        raise HTTPException(502, f"Comfly chat 调用失败：{e}")

    # 按 usage 结算
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    actual = estimate_comfly_credits(model, {"usage": usage}, for_user=True) or estimated
    _do_settle(db, current_user, pre=pre, actual=int(actual),
               capability_id=_CAPABILITY_FOR_BILLING, model=model, endpoint="chat",
               extra_meta={"usage": usage})
    _audit("chat_settled", user_id=current_user.id, model=model,
           pre=credits_json_float(pre), actual=int(actual), usage=usage)
    return JSONResponse(resp)


@router.post("/api/comfly-proxy/v1/images/generations", summary="Comfly images 透明 proxy（按 per_call 计费）")
async def proxy_images_generations(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_request_authorized_for_billing(request)
    body = await request.json()
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "缺少 model")
    if not lookup_comfly_model(model):
        raise HTTPException(400, f"模型 {model} 未在 comfly_pricing.json 注册，无法计费")

    estimated = estimate_comfly_credits(model, body, for_user=True) or 1
    pre = _do_pre_deduct(db, current_user, estimated,
                         capability_id=_CAPABILITY_FOR_BILLING, model=model, endpoint="image")
    _audit("image_pre_deduct", user_id=current_user.id, model=model, estimated=estimated)

    try:
        resp = await _comfly_request("POST", _comfly_url("/v1/images/generations", model),
                                     body, _comfly_headers(model), _TIMEOUT_IMAGE)
    except Exception as e:
        _do_full_refund(db, current_user, pre=pre,
                        capability_id=_CAPABILITY_FOR_BILLING, model=model, endpoint="image", error=str(e))
        _audit("image_failed", user_id=current_user.id, model=model, error=str(e)[:300])
        raise HTTPException(502, f"Comfly images 调用失败：{e}")

    # per_call 估算 == 实际，无需 settle
    _audit("image_ok", user_id=current_user.id, model=model, pre=credits_json_float(pre))
    return JSONResponse(resp)


@router.post("/api/comfly-proxy/v2/videos/generations", summary="Comfly Veo 视频提交 proxy（按 per_call 预扣）")
async def proxy_videos_generations_submit(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_request_authorized_for_billing(request)
    body = await request.json()
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "缺少 model")
    if not lookup_comfly_model(model):
        raise HTTPException(400, f"模型 {model} 未在 comfly_pricing.json 注册，无法计费")

    estimated = estimate_comfly_credits(model, body, for_user=True) or 1
    pre = _do_pre_deduct(db, current_user, estimated,
                         capability_id=_CAPABILITY_FOR_BILLING, model=model, endpoint="video_submit")
    _audit("video_submit_pre_deduct", user_id=current_user.id, model=model, estimated=estimated)

    try:
        resp = await _comfly_request("POST", _comfly_url("/v2/videos/generations", model),
                                     body, _comfly_headers(model), _TIMEOUT_VIDEO_SUBMIT)
    except Exception as e:
        _do_full_refund(db, current_user, pre=pre,
                        capability_id=_CAPABILITY_FOR_BILLING, model=model, endpoint="video_submit", error=str(e))
        _audit("video_submit_failed", user_id=current_user.id, model=model, error=str(e)[:300])
        raise HTTPException(502, f"Comfly videos submit 调用失败：{e}")

    # 注意：Veo submit 即扣，后续 poll 失败暂不退款（pipeline runner 自己重试，且任务通常会跑成功）
    # 如果未来要"任务最终 failed 才退款"，需要在 poll 端点检测 status 后回填 refund，并加 task_id → pre 的映射存储
    _audit("video_submit_ok", user_id=current_user.id, model=model,
           task_id=(resp.get("data", {}) or {}).get("task_id") if isinstance(resp.get("data"), dict) else resp.get("task_id"),
           pre=credits_json_float(pre))
    return JSONResponse(resp)


@router.get("/api/comfly-proxy/v2/videos/generations/{task_id}", summary="Comfly Veo 任务轮询 proxy（不计费）")
async def proxy_videos_generations_poll(
    task_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    _check_request_authorized_for_billing(request)
    # poll 不计费，不需要 model 路由（默认 token group），但 Comfly 实际不区分
    try:
        resp = await _comfly_request("GET", _comfly_url(f"/v2/videos/generations/{task_id}"),
                                     None, _comfly_headers(), _TIMEOUT_VIDEO_POLL)
    except Exception as e:
        raise HTTPException(502, f"Comfly videos poll 调用失败：{e}")
    return JSONResponse(resp)
