"""能力注册、调用日志与用户积分变更（与速推同扣）。

动账接口 /capabilities/pre-deduct、record-call、refund 的**唯一实现**在本模块；应由 **实际调用速推的 MCP**
invoke_capability 顺序触发，不在此之外再实现第二套扣费。
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from .auth import get_current_user, brand_mark_for_jwt_claim
from ..models import BillingIdempotency, CapabilityCallLog, CapabilityConfig, User
from ..services.credit_ledger import append_credit_ledger
from ..services.credits_amount import credits_json_float, quantize_credits, user_balance_decimal
from ..services.sutui_api_audit import log_capability_call_log_persisted
from .installation_slots import installation_slots_enabled, parse_installation_id_strict
from .skills import user_can_use_capability

router = APIRouter()

_PRICING_JSON_PATH = Path(__file__).resolve().parent.parent.parent.parent / "comfly_pricing.json"


def _get_user_price_multiplier() -> float:
    """用户消耗 = 采购价 × 倍率。优先环境变量，其次 comfly_pricing.json，默认 3。"""
    env_val = os.environ.get("USER_PRICE_MULTIPLIER", "").strip()
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    try:
        if _PRICING_JSON_PATH.exists():
            data = json.loads(_PRICING_JSON_PATH.read_text("utf-8"))
            return float(data.get("user_price_multiplier_default", 3))
    except Exception:
        pass
    return 3.0


def _installation_id_for_capability_checks(x_installation_id: Optional[str]) -> Optional[str]:
    """在线版槽位开启时校验并返回 installation_id；否则返回 None。"""
    if not installation_slots_enabled():
        return None
    return parse_installation_id_strict(x_installation_id)


def _should_deduct_credits() -> bool:
    """是否启用「调用能力时扣积分」（在线版 + 独立认证时）。"""
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    return edition == "online" and getattr(settings, "lobster_independent_auth", True)


def _require_sutui_brand_for_billing(user: User, *, upstream: str) -> None:
    """速推上游计费时 JWT 须为 bihuo/yingshi；无品牌或非两池不允许预扣/按次扣费。"""
    if not _should_deduct_credits():
        return
    if (upstream or "").strip() != "sutui":
        return
    bm = brand_mark_for_jwt_claim(getattr(user, "brand_mark", None))
    if bm not in ("bihuo", "yingshi"):
        raise HTTPException(
            status_code=403,
            detail=(
                "账号未绑定必火/影视品牌，无法使用速推算力；无通用兜底。"
                "请使用对应品牌客户端注册或联系管理员补全品牌后重新登录。"
            ),
        )


def _billing_request_may_mutate_balance(request: Request) -> bool:
    """
    仅本机 MCP（直连 Backend 的 127.0.0.1/::1）或携带 X-Lobster-Mcp-Billing 与 LOBSTER_MCP_BILLING_INTERNAL_KEY 一致时，
    对 pre-deduct / record-call / refund 做实质积分变更。
    在线独立认证且需扣费时：不满足上述条件则返回 403，禁止「未扣费即生成」。
    """
    k = (getattr(settings, "lobster_mcp_billing_internal_key", None) or "").strip()
    h = (request.headers.get("X-Lobster-Mcp-Billing") or "").strip()
    ch = getattr(request.client, "host", None) or ""
    loopback = ch in ("127.0.0.1", "::1", "localhost")
    if k:
        return h == k or loopback
    return loopback


@router.get("/capabilities/available", summary="当前可用能力列表（含付费技能限制：未解锁的付费技能不会出现在列表中）")
def list_available(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    x_installation_id: Optional[str] = Header(None, alias="X-Installation-Id"),
):
    iid = _installation_id_for_capability_checks(x_installation_id)
    rows = db.query(CapabilityConfig).filter(CapabilityConfig.enabled.is_(True)).order_by(CapabilityConfig.capability_id).all()
    out = []
    for r in rows:
        if not user_can_use_capability(db, current_user.id, r.capability_id, iid):
            continue
        out.append({
            "capability_id": r.capability_id,
            "description": r.description,
            "upstream": r.upstream,
            "upstream_tool": r.upstream_tool,
            "arg_schema": r.arg_schema,
            "extra_config": getattr(r, "extra_config", None),
            "is_default": r.is_default,
            "unit_credits": r.unit_credits,
        })
    return {"capabilities": out}


@router.get("/capabilities/registry", summary="能力注册列表（需登录）")
def list_registry(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.query(CapabilityConfig).order_by(CapabilityConfig.capability_id).all()
    return [
        {
            "capability_id": r.capability_id,
            "description": r.description,
            "upstream": r.upstream,
            "upstream_tool": r.upstream_tool,
            "enabled": r.enabled,
            "is_default": r.is_default,
            "unit_credits": r.unit_credits,
            "extra_config": getattr(r, "extra_config", None),
        }
        for r in rows
    ]


class RecordCallIn(BaseModel):
    capability_id: str
    success: bool = True
    latency_ms: Optional[int] = None
    request_payload: Optional[dict] = None
    response_payload: Optional[dict] = None
    error_message: Optional[str] = None
    source: str = "mcp_invoke"
    chat_session_id: Optional[str] = None
    chat_context_id: Optional[str] = None
    """若由 pre-deduct 已扣过，传本次扣费数；pre_deduct_applied=True 时不在本接口再次减余额。"""
    credits_charged: Optional[float] = None
    pre_deduct_applied: bool = False
    """预扣金额（与 credits_charged 在预扣成功时一致）；与 credits_final 联用做差额结算。"""
    credits_pre_deducted: Optional[float] = None
    """速推返回的实际消耗积分；与预扣差额多退少补。"""
    credits_final: Optional[float] = None
    """站内对账：本次上游选用的池与 token 指纹（仅 MCP 本机计费传入；不入用户 API）。"""
    sutui_pool: Optional[str] = None
    sutui_token_ref: Optional[str] = None


class PreDeductIn(BaseModel):
    capability_id: str
    model: Optional[str] = None
    params: Optional[dict] = None
    sutui_pool: Optional[str] = None
    sutui_token_ref: Optional[str] = None
    force_credits: Optional[float] = None


def _sutui_recon_for_ledger(
    request: Request,
    *,
    upstream: str,
    sutui_pool: Optional[str],
    sutui_token_ref: Optional[str],
) -> dict:
    """写入 meta._recon；仅本机 MCP 计费且 upstream 为 sutui 时采纳。"""
    if not _billing_request_may_mutate_balance(request):
        return {}
    if (upstream or "").strip() != "sutui":
        return {}
    sp = (sutui_pool or "").strip()
    ref = (sutui_token_ref or "").strip()
    if not sp or not ref:
        return {}
    return {"_recon": {"sutui_pool": sp, "sutui_token_ref": ref}}


def _billing_idempotency_key(request: Request) -> str:
    return (
        (request.headers.get("X-Billing-Idempotency-Key") or request.headers.get("X-Idempotency-Key") or "")
        .strip()[:128]
    )


def _pre_deduct_idempotent_cached(
    db: Session, user_id: int, idem_key: str
) -> Optional[dict]:
    if not idem_key:
        return None
    row = (
        db.query(BillingIdempotency)
        .filter(
            BillingIdempotency.user_id == user_id,
            BillingIdempotency.key == idem_key,
            BillingIdempotency.endpoint == "pre_deduct",
        )
        .first()
    )
    if not row:
        return None
    if datetime.utcnow() - row.created_at > timedelta(minutes=10):
        return None
    try:
        return json.loads(row.response_json)
    except Exception:
        return None


def _pre_deduct_idempotent_store(db: Session, user_id: int, idem_key: str, payload: dict) -> None:
    if not idem_key:
        return
    try:
        db.add(
            BillingIdempotency(
                user_id=user_id,
                key=idem_key,
                endpoint="pre_deduct",
                response_json=json.dumps(payload, ensure_ascii=False),
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
    except Exception:
        db.rollback()


@router.post("/capabilities/pre-deduct", summary="调用能力前预扣积分（不足返回 402）")
def pre_deduct(
    body: PreDeductIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    x_installation_id: Optional[str] = Header(None, alias="X-Installation-Id"),
):
    idem_key = _billing_idempotency_key(request)
    iid = _installation_id_for_capability_checks(x_installation_id)
    if not user_can_use_capability(db, current_user.id, body.capability_id, iid):
        raise HTTPException(
            status_code=403,
            detail="该能力属于付费技能，请先在技能商店付费解锁后再使用。",
        )
    if not _should_deduct_credits():
        return {"credits_charged": 0, "message": "未启用积分扣减"}
    if not _billing_request_may_mutate_balance(request):
        raise HTTPException(
            status_code=403,
            detail=(
                "计费请求来源未受信任（非本机回环且未携带有效 X-Lobster-Mcp-Billing），"
                "拒绝预扣以免未扣费即调用上游。请为 MCP/网关配置与认证中心一致的 LOBSTER_MCP_BILLING_INTERNAL_KEY，"
                "并在请求认证中心 /capabilities/pre-deduct（及 record-call、refund）时带上请求头 X-Lobster-Mcp-Billing。"
            ),
        )
    if idem_key:
        cached = _pre_deduct_idempotent_cached(db, current_user.id, idem_key)
        if cached is not None:
            return cached
    cap = db.query(CapabilityConfig).filter(CapabilityConfig.capability_id == body.capability_id).first()
    upstream = (cap.upstream or "").strip() if cap else ""
    upstream_tool = (cap.upstream_tool or "").strip() if cap else ""
    _require_sutui_brand_for_billing(current_user, upstream=upstream)

    # ── force_credits: MCP 已算好金额（Comfly 路由等场景）──
    if body.force_credits is not None and body.force_credits > 0:
        fc = quantize_credits(body.force_credits)
        db.refresh(current_user)
        if user_balance_decimal(current_user) < fc:
            raise HTTPException(
                status_code=402,
                detail=f"积分不足：本次需 {float(fc)} 积分，当前余额 {float(user_balance_decimal(current_user))}。请先充值。",
            )
        current_user.credits = user_balance_decimal(current_user) - fc
        bal = quantize_credits(current_user.credits)
        _recon_fc = _sutui_recon_for_ledger(
            request,
            upstream="comfly",
            sutui_pool=body.sutui_pool,
            sutui_token_ref=body.sutui_token_ref,
        )
        append_credit_ledger(
            db,
            current_user.id,
            -fc,
            "pre_deduct",
            bal,
            description="能力预扣（Comfly 固定价）",
            ref_type="capability",
            meta={
                **(_recon_fc or {}),
                "capability_id": body.capability_id,
                "model": body.model or "",
                "pre_estimated": credits_json_float(fc),
                "upstream": "comfly",
            },
        )
        db.commit()
        out = {"credits_charged": credits_json_float(fc)}
        _pre_deduct_idempotent_store(db, current_user.id, idem_key, out)
        return out

    _UNDERSTAND_CAPS = ("image.understand", "video.understand")
    if upstream == "sutui" and upstream_tool == "generate" and body.capability_id not in _UNDERSTAND_CAPS:
        from ..services.sutui_billing_gate import assert_pricing_pre_deduct_allows_upstream_or_http

        model = (body.model or "").strip()
        if not model:
            raise HTTPException(
                status_code=400,
                detail="调用生成能力时必须提供 model 以按速推定价预扣积分。",
            )
        params = body.params if isinstance(body.params, dict) else None
        est_d = assert_pricing_pre_deduct_allows_upstream_or_http(
            db,
            current_user,
            model,
            params,
            action_label="素材生成",
        )
        _multiplier = _get_user_price_multiplier()
        est_d = quantize_credits(float(est_d) * _multiplier)
        current_user.credits = user_balance_decimal(current_user) - est_d
        bal = quantize_credits(current_user.credits)
        _recon = _sutui_recon_for_ledger(
            request,
            upstream=upstream,
            sutui_pool=body.sutui_pool,
            sutui_token_ref=body.sutui_token_ref,
        )
        append_credit_ledger(
            db,
            current_user.id,
            -est_d,
            "pre_deduct",
            bal,
            description=f"能力预扣（按模型估价×{_multiplier:.0f}）",
            ref_type="capability",
            meta={
                **(_recon or {}),
                "capability_id": body.capability_id,
                "model": model,
                "pre_estimated": credits_json_float(est_d),
                "price_multiplier": _multiplier,
            },
        )
        db.commit()
        out = {"credits_charged": credits_json_float(est_d)}
        _pre_deduct_idempotent_store(db, current_user.id, idem_key, out)
        return out

    unit_credits = int(cap.unit_credits or 0) if cap else 0
    if unit_credits <= 0:
        return {"credits_charged": 0}
    db.refresh(current_user)
    uc = quantize_credits(unit_credits)
    if user_balance_decimal(current_user) < uc:
        raise HTTPException(
            status_code=402,
            detail=f"积分不足：本次需 {unit_credits} 积分，当前余额 {user_balance_decimal(current_user)}。请先充值。",
        )
    current_user.credits = user_balance_decimal(current_user) - uc
    bal = quantize_credits(current_user.credits)
    _recon_u = _sutui_recon_for_ledger(
        request,
        upstream=upstream,
        sutui_pool=body.sutui_pool,
        sutui_token_ref=body.sutui_token_ref,
    )
    append_credit_ledger(
        db,
        current_user.id,
        -uc,
        "pre_deduct",
        bal,
        description="能力预扣（按 unit_credits）",
        ref_type="capability",
        meta={
            **(_recon_u or {}),
            "capability_id": body.capability_id,
            "unit_credits": unit_credits,
        },
    )
    db.commit()
    out = {"credits_charged": unit_credits}
    _pre_deduct_idempotent_store(db, current_user.id, idem_key, out)
    return out


class RefundIn(BaseModel):
    capability_id: str
    credits: float
    sutui_pool: Optional[str] = None
    sutui_token_ref: Optional[str] = None


@router.post("/capabilities/refund", summary="调用失败时退还预扣积分")
def refund_credits(
    body: RefundIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _should_deduct_credits() or body.credits <= 0:
        return {"ok": True}
    if not _billing_request_may_mutate_balance(request):
        raise HTTPException(
            status_code=403,
            detail=(
                "退费请求来源未受信任，拒绝执行以免账务不一致。"
                "请配置 LOBSTER_MCP_BILLING_INTERNAL_KEY 并转发 X-Lobster-Mcp-Billing。"
            ),
        )
    db.refresh(current_user)
    refund_amt = quantize_credits(body.credits)
    current_user.credits = user_balance_decimal(current_user) + refund_amt
    bal = quantize_credits(current_user.credits)
    _recon_rf = _sutui_recon_for_ledger(
        request,
        upstream="sutui",
        sutui_pool=body.sutui_pool,
        sutui_token_ref=body.sutui_token_ref,
    )
    append_credit_ledger(
        db,
        current_user.id,
        refund_amt,
        "refund",
        bal,
        description="预扣/任务失败退款",
        ref_type="capability",
        meta={
            **(_recon_rf or {}),
            "capability_id": body.capability_id,
            "refund_credits": float(refund_amt),
        },
    )
    db.commit()
    return {"ok": True, "refunded": float(refund_amt)}


@router.post("/capabilities/record-call", summary="记录能力调用（独立认证时按 unit_credits 扣积分，或使用 pre-deduct 已扣数量）")
def record_call(
    body: RecordCallIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    x_installation_id: Optional[str] = Header(None, alias="X-Installation-Id"),
):
    iid = _installation_id_for_capability_checks(x_installation_id)
    if not user_can_use_capability(db, current_user.id, body.capability_id, iid):
        raise HTTPException(
            status_code=403,
            detail="该能力属于付费技能，请先在技能商店付费解锁后再使用。",
        )
    if _should_deduct_credits() and not _billing_request_may_mutate_balance(request):
        raise HTTPException(
            status_code=403,
            detail=(
                "结算请求来源未受信任（非本机回环且未携带有效 X-Lobster-Mcp-Billing），"
                "拒绝记录调用/扣费以免未记账即完成生成。请配置 LOBSTER_MCP_BILLING_INTERNAL_KEY 并转发 X-Lobster-Mcp-Billing。"
            ),
        )
    cap = db.query(CapabilityConfig).filter(CapabilityConfig.capability_id == body.capability_id).first()
    upstream_rc = (getattr(cap, "upstream", None) or "").strip() if cap else ""
    _require_sutui_brand_for_billing(current_user, upstream=upstream_rc)
    unit_credits = int(cap.unit_credits or 0) if cap else 0
    credits_charged_body = quantize_credits(body.credits_charged if body.credits_charged is not None else 0)
    pre_applied = bool(getattr(body, "pre_deduct_applied", False))
    credits_final = getattr(body, "credits_final", None)
    credits_pre_deducted = getattr(body, "credits_pre_deducted", None)

    credits_charged = quantize_credits(0)
    db.refresh(current_user)
    balance_before = user_balance_decimal(current_user)
    ledger_kind: Optional[str] = None
    ledger_meta: Optional[dict] = None

    if _should_deduct_credits() and pre_applied and credits_final is not None:
        pre = quantize_credits(credits_pre_deducted if credits_pre_deducted is not None else credits_charged_body)
        final = quantize_credits(credits_final)
        delta = final - pre
        if delta > 0 and user_balance_decimal(current_user) < delta:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"积分不足：速推实际扣费 {final}，预扣 {pre}，需补扣 {delta} 积分，"
                    f"当前余额 {user_balance_decimal(current_user)}。请先充值。"
                ),
            )
        current_user.credits = user_balance_decimal(current_user) - delta
        credits_charged = final
        ledger_kind = "settle"
        ledger_meta = {
            "capability_id": body.capability_id,
            "pre_deducted": pre,
            "final": final,
            "delta_settle": -delta,
        }
    elif credits_charged_body > 0 and _should_deduct_credits() and not pre_applied:
        if user_balance_decimal(current_user) < credits_charged_body:
            raise HTTPException(
                status_code=402,
                detail=f"积分不足：本次需 {credits_charged_body} 积分（速推返回消耗），当前余额 {user_balance_decimal(current_user)}。请先充值。",
            )
        current_user.credits = user_balance_decimal(current_user) - credits_charged_body
        credits_charged = credits_charged_body
        ledger_kind = "direct_charge"
        ledger_meta = {"capability_id": body.capability_id, "credits_charged": credits_charged_body}
    elif credits_charged_body == 0 and _should_deduct_credits() and not pre_applied and unit_credits > 0:
        uc = quantize_credits(unit_credits)
        if user_balance_decimal(current_user) < uc:
            raise HTTPException(
                status_code=402,
                detail=f"积分不足：本次需 {unit_credits} 积分，当前余额 {user_balance_decimal(current_user)}。请先充值。",
            )
        current_user.credits = user_balance_decimal(current_user) - uc
        credits_charged = uc
        ledger_kind = "unit_charge"
        ledger_meta = {"capability_id": body.capability_id, "unit_credits": unit_credits}
    elif pre_applied and credits_final is None and credits_charged_body > 0:
        credits_charged = credits_charged_body
    log = CapabilityCallLog(
        user_id=current_user.id,
        capability_id=body.capability_id,
        upstream=cap.upstream if cap else None,
        upstream_tool=cap.upstream_tool if cap else None,
        success=body.success,
        credits_charged=credits_charged,
        latency_ms=body.latency_ms,
        request_payload=body.request_payload,
        response_payload=body.response_payload,
        error_message=(body.error_message or "")[:1000] or None,
        source=body.source,
        chat_session_id=(body.chat_session_id or "")[:128] or None,
        chat_context_id=(body.chat_context_id or "")[:128] or None,
    )
    db.add(log)
    db.flush()
    balance_after = quantize_credits(current_user.credits)
    ldelta = balance_after - balance_before
    if ledger_kind:
        _recon_r = _sutui_recon_for_ledger(
            request,
            upstream=upstream_rc,
            sutui_pool=body.sutui_pool,
            sutui_token_ref=body.sutui_token_ref,
        )
        append_credit_ledger(
            db,
            current_user.id,
            ldelta,
            ledger_kind,
            balance_after,
            description=f"能力调用结算 {body.capability_id}",
            ref_type="capability_call_log",
            ref_id=str(log.id),
            meta={
                **(_recon_r or {}),
                **(ledger_meta or {}),
                "success": body.success,
                "credits_charged": credits_json_float(credits_charged),
            },
        )
    db.commit()
    db.refresh(log)
    try:
        req_sum = body.request_payload
        if isinstance(req_sum, dict) and len(json.dumps(req_sum, default=str)) > 8000:
            req_sum = {k: req_sum.get(k) for k in ("capability_id", "model", "task_id", "payload") if k in req_sum}
        log_capability_call_log_persisted(
            log_id=log.id,
            user_id=current_user.id,
            capability_id=log.capability_id or "",
            credits_charged=credits_charged,
            success=bool(body.success),
            source=(body.source or "")[:64],
            request_summary=req_sum,
            error_message=log.error_message,
        )
    except Exception:
        pass
    return {
        "id": log.id,
        "capability_id": log.capability_id,
        "success": log.success,
        "credits_charged": credits_json_float(credits_charged),
    }


@router.get("/capabilities/my-call-logs", summary="我的能力调用记录")
def my_call_logs(
    capability_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(CapabilityCallLog).filter(CapabilityCallLog.user_id == current_user.id)
    if capability_id:
        q = q.filter(CapabilityCallLog.capability_id == capability_id)
    rows = q.order_by(CapabilityCallLog.created_at.desc()).offset(max(offset, 0)).limit(min(max(limit, 1), 200)).all()
    return [
        {
            "id": r.id,
            "capability_id": r.capability_id,
            "success": r.success,
            "credits_charged": credits_json_float(r.credits_charged),
            "latency_ms": r.latency_ms,
            "request_payload": r.request_payload,
            "response_payload": r.response_payload,
            "error_message": r.error_message,
            "source": r.source,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]


@router.get("/capabilities/comfly-pricing", summary="Comfly 定价表（供 lobster_online 算力确认使用）")
def comfly_pricing():
    """返回 comfly_pricing.json 内容，前端可据此判断哪些模型走 Comfly 并展示预估算力。"""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(__file__).resolve().parent.parent.parent.parent / "comfly_pricing.json"
    if not p.exists():
        return {"models": {}}
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"models": {}}
