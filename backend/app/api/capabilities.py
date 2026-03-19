"""Capabilities: list available capabilities and call logs；调用时按 unit_credits 扣积分（与速推同扣）。付费技能仅已解锁用户可用。"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from .auth import get_current_user
from ..models import CapabilityCallLog, CapabilityConfig, User
from .skills import user_can_use_capability

router = APIRouter()


def _should_deduct_credits() -> bool:
    """是否启用「调用能力时扣积分」（在线版 + 独立认证时）。"""
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    return edition == "online" and getattr(settings, "lobster_independent_auth", True)


@router.get("/capabilities/available", summary="当前可用能力列表（含付费技能限制：未解锁的付费技能不会出现在列表中）")
def list_available(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.query(CapabilityConfig).filter(CapabilityConfig.enabled.is_(True)).order_by(CapabilityConfig.capability_id).all()
    out = []
    for r in rows:
        if not user_can_use_capability(db, current_user.id, r.capability_id):
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
    credits_charged: Optional[int] = None
    pre_deduct_applied: bool = False


class PreDeductIn(BaseModel):
    capability_id: str


@router.post("/capabilities/pre-deduct", summary="调用能力前预扣积分（不足返回 402）")
def pre_deduct(
    body: PreDeductIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user_can_use_capability(db, current_user.id, body.capability_id):
        raise HTTPException(
            status_code=403,
            detail="该能力属于付费技能，请先在技能商店付费解锁后再使用。",
        )
    if not _should_deduct_credits():
        return {"credits_charged": 0, "message": "未启用积分扣减"}
    cap = db.query(CapabilityConfig).filter(CapabilityConfig.capability_id == body.capability_id).first()
    unit_credits = int(cap.unit_credits or 0) if cap else 0
    if unit_credits <= 0:
        return {"credits_charged": 0}
    db.refresh(current_user)
    if (current_user.credits or 0) < unit_credits:
        raise HTTPException(
            status_code=402,
            detail=f"积分不足：本次需 {unit_credits} 积分，当前余额 {current_user.credits or 0}。请先充值。",
        )
    current_user.credits = (current_user.credits or 0) - unit_credits
    db.commit()
    return {"credits_charged": unit_credits}


class RefundIn(BaseModel):
    capability_id: str
    credits: int


@router.post("/capabilities/refund", summary="调用失败时退还预扣积分")
def refund_credits(
    body: RefundIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _should_deduct_credits() or body.credits <= 0:
        return {"ok": True}
    db.refresh(current_user)
    current_user.credits = (current_user.credits or 0) + body.credits
    db.commit()
    return {"ok": True, "refunded": body.credits}


@router.post("/capabilities/record-call", summary="记录能力调用（独立认证时按 unit_credits 扣积分，或使用 pre-deduct 已扣数量）")
def record_call(
    body: RecordCallIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user_can_use_capability(db, current_user.id, body.capability_id):
        raise HTTPException(
            status_code=403,
            detail="该能力属于付费技能，请先在技能商店付费解锁后再使用。",
        )
    cap = db.query(CapabilityConfig).filter(CapabilityConfig.capability_id == body.capability_id).first()
    unit_credits = int(cap.unit_credits or 0) if cap else 0
    credits_charged = body.credits_charged if body.credits_charged is not None else 0
    pre_applied = bool(getattr(body, "pre_deduct_applied", False))

    if credits_charged > 0 and _should_deduct_credits():
        if pre_applied:
            pass
        else:
            db.refresh(current_user)
            if (current_user.credits or 0) < credits_charged:
                raise HTTPException(
                    status_code=402,
                    detail=f"积分不足：本次需 {credits_charged} 积分（速推返回消耗），当前余额 {current_user.credits or 0}。请先充值。",
                )
            current_user.credits = (current_user.credits or 0) - credits_charged
    elif credits_charged == 0 and _should_deduct_credits() and unit_credits > 0:
        db.refresh(current_user)
        if (current_user.credits or 0) < unit_credits:
            raise HTTPException(
                status_code=402,
                detail=f"积分不足：本次需 {unit_credits} 积分，当前余额 {current_user.credits or 0}。请先充值。",
            )
        current_user.credits = (current_user.credits or 0) - unit_credits
        credits_charged = unit_credits
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
    db.commit()
    db.refresh(log)
    return {"id": log.id, "capability_id": log.capability_id, "success": log.success, "credits_charged": credits_charged}


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
            "credits_charged": r.credits_charged,
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


