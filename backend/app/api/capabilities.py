"""Capabilities: list available capabilities and call logs；调用时按 unit_credits 扣积分（与速推同扣）。付费技能仅已解锁用户可用。"""
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from .auth import get_current_user
from ..models import BillingIdempotency, CapabilityCallLog, CapabilityConfig, User
from ..services.credit_ledger import append_credit_ledger
from ..services.credits_amount import credits_json_float, quantize_credits, user_balance_decimal
from .installation_slots import installation_slots_enabled, parse_installation_id_strict
from .skills import user_can_use_capability

router = APIRouter()


def _installation_id_for_capability_checks(x_installation_id: Optional[str]) -> Optional[str]:
    """在线版槽位开启时校验并返回 installation_id；否则返回 None。"""
    if not installation_slots_enabled():
        return None
    return parse_installation_id_strict(x_installation_id)


def _should_deduct_credits() -> bool:
    """是否启用「调用能力时扣积分」（在线版 + 独立认证时）。"""
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    return edition == "online" and getattr(settings, "lobster_independent_auth", True)


def _billing_request_may_mutate_balance(request: Request) -> bool:
    """
    仅本机 MCP（直连 Backend 的 127.0.0.1/::1）或携带 X-Lobster-Mcp-Billing 与 LOBSTER_MCP_BILLING_INTERNAL_KEY 一致时，
    对 pre-deduct / record-call / refund 做实质积分变更；其它来源（公网直连、本机代理转发）返回 billing_skipped，避免与 MCP 重复扣费。
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


class PreDeductIn(BaseModel):
    capability_id: str
    model: Optional[str] = None
    params: Optional[dict] = None


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
        return {"credits_charged": 0, "billing_skipped": True}
    if idem_key:
        cached = _pre_deduct_idempotent_cached(db, current_user.id, idem_key)
        if cached is not None:
            return cached
    cap = db.query(CapabilityConfig).filter(CapabilityConfig.capability_id == body.capability_id).first()
    upstream = (cap.upstream or "").strip() if cap else ""
    upstream_tool = (cap.upstream_tool or "").strip() if cap else ""

    if upstream == "sutui" and upstream_tool == "generate":
        from ..services.sutui_pricing import estimate_pre_deduct_credits

        model = (body.model or "").strip()
        if not model:
            raise HTTPException(
                status_code=400,
                detail="调用生成能力时必须提供 model 以按速推定价预扣积分。",
            )
        est, err = estimate_pre_deduct_credits(model, body.params if isinstance(body.params, dict) else None)
        if err:
            raise HTTPException(status_code=400, detail=err)
        db.refresh(current_user)
        est_d = quantize_credits(est)
        if user_balance_decimal(current_user) < est_d:
            raise HTTPException(
                status_code=402,
                detail=f"积分不足：本次预估需 {est} 积分（按速推模型定价），当前余额 {user_balance_decimal(current_user)}。请先充值。",
            )
        current_user.credits = user_balance_decimal(current_user) - est_d
        bal = quantize_credits(current_user.credits)
        append_credit_ledger(
            db,
            current_user.id,
            -est_d,
            "pre_deduct",
            bal,
            description="能力预扣（按模型估价）",
            ref_type="capability",
            meta={
                "capability_id": body.capability_id,
                "model": model,
                "pre_estimated": est,
            },
        )
        db.commit()
        out = {"credits_charged": est}
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
    append_credit_ledger(
        db,
        current_user.id,
        -uc,
        "pre_deduct",
        bal,
        description="能力预扣（按 unit_credits）",
        ref_type="capability",
        meta={"capability_id": body.capability_id, "unit_credits": unit_credits},
    )
    db.commit()
    out = {"credits_charged": unit_credits}
    _pre_deduct_idempotent_store(db, current_user.id, idem_key, out)
    return out


class RefundIn(BaseModel):
    capability_id: str
    credits: float


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
        return {"ok": True, "billing_skipped": True, "refunded": 0}
    db.refresh(current_user)
    refund_amt = quantize_credits(body.credits)
    current_user.credits = user_balance_decimal(current_user) + refund_amt
    bal = quantize_credits(current_user.credits)
    append_credit_ledger(
        db,
        current_user.id,
        refund_amt,
        "refund",
        bal,
        description="预扣/任务失败退款",
        ref_type="capability",
        meta={"capability_id": body.capability_id, "refund_credits": float(refund_amt)},
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
        return {
            "id": 0,
            "capability_id": body.capability_id,
            "success": body.success,
            "credits_charged": 0,
            "billing_skipped": True,
        }
    cap = db.query(CapabilityConfig).filter(CapabilityConfig.capability_id == body.capability_id).first()
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
                **(ledger_meta or {}),
                "success": body.success,
                "credits_charged": credits_json_float(credits_charged),
            },
        )
    db.commit()
    db.refresh(log)
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


