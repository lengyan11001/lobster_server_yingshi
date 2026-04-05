"""速推相关：出站 HTTP 与积分流水审计日志（[sutui-audit] 前缀，便于 grep）。"""
from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal
from typing import Any, Dict, Optional

logger = logging.getLogger("sutui_audit")

_UPSTREAM_AUDIT_MAX = 500_000

_AUDITED_LEDGER_ENTRY_TYPES = frozenset(
    {
        "sutui_chat",
        "pre_deduct",
        "settle",
        "refund",
        "direct_charge",
        "unit_charge",
        "recharge",
    }
)


def _json_clip(obj: Any, max_len: int = 12000) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    if len(s) > max_len:
        return s[:max_len] + f"... [truncated total_len={len(s)}]"
    return s


def _token_ref(secret: str) -> str:
    t = (secret or "").strip()
    if not t:
        return ""
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:12]


def _upstream_body_for_audit(obj: Any) -> str:
    """对方返回正文：字符串原样截断，对象则 JSON（与 mcp 侧 _SUTUI_UPSTREAM_LOG_MAX 量级一致）。"""
    if isinstance(obj, str):
        s = obj
        if len(s) > _UPSTREAM_AUDIT_MAX:
            return s[:_UPSTREAM_AUDIT_MAX] + f"... [truncated total_len={len(s)}]"
        return s
    return _json_clip(obj, _UPSTREAM_AUDIT_MAX)


def log_xskill_http(
    *,
    phase: str,
    method: str,
    url: str,
    http_status: int,
    capability_or_model: str = "",
    billing_snapshot: Optional[Dict[str, Any]] = None,
    error_message: str = "",
    extra: Optional[Dict[str, Any]] = None,
    bearer_token: Optional[str] = None,
    sutui_pool: str = "",
    upstream_response: Optional[Any] = None,
) -> None:
    """记录每一次发往 xskill 的 HTTP（或等价）调用：池、完整 sk、token 摘要、计费快照与对方响应。"""
    tok = (bearer_token or "").strip()
    ref = _token_ref(tok) or "-"
    pool = (sutui_pool or "").strip() or "-"
    tok_out = tok if tok else "-"
    logger.info(
        "[sutui-audit] http phase=%s %s %s http=%s pool=%s sutui_token_ref=%s bearer_token=%s cap_or_model=%s billing=%s err=%s extra=%s",
        phase,
        method,
        url,
        http_status,
        pool,
        ref,
        tok_out,
        capability_or_model or "-",
        _json_clip(billing_snapshot) if billing_snapshot else "-",
        (error_message or "-")[:8000],
        _json_clip(extra, 12000) if extra else "-",
    )
    if upstream_response is not None:
        logger.info(
            "[sutui-audit] upstream_body phase=%s pool=%s sutui_token_ref=%s body=%s",
            phase,
            pool,
            ref,
            _upstream_body_for_audit(upstream_response),
        )

    # 固定给人看的两行：本请求用的哪把 sk、对方完整返回（grep [sutui-exchange]）
    if upstream_response is not None:
        _exchange_body = _upstream_body_for_audit(upstream_response)
    elif (error_message or "").strip():
        em = (error_message or "").strip()
        _exchange_body = em[:_UPSTREAM_AUDIT_MAX] + (
            f"... [truncated total_len={len(em)}]" if len(em) > _UPSTREAM_AUDIT_MAX else ""
        )
    elif billing_snapshot:
        _exchange_body = _json_clip(billing_snapshot, _UPSTREAM_AUDIT_MAX)
    else:
        _exchange_body = "-"
    logger.info(
        "[sutui-exchange] 本请求使用的 Bearer（完整 token）=%s pool=%s token_ref=%s phase=%s",
        tok_out,
        pool,
        ref,
        phase,
    )
    logger.info(
        "[sutui-exchange] %s %s HTTP=%s cap_or_model=%s 对方返回正文=%s",
        method,
        url,
        http_status,
        capability_or_model or "-",
        _exchange_body,
    )


def maybe_log_credit_ledger_append(
    *,
    user_id: int,
    entry_type: str,
    delta: Decimal,
    balance_after: Decimal,
    ref_type: Optional[str],
    ref_id: Optional[str],
    description: str,
    meta: Optional[Dict[str, Any]],
) -> None:
    """能力与速推相关流水入库时打印（含 meta，便于对账）。"""
    et = (entry_type or "").strip()
    rt = (ref_type or "").strip()
    if et not in _AUDITED_LEDGER_ENTRY_TYPES:
        if rt not in ("capability_call_log", "sutui_chat") and not (rt == "capability" and et == "pre_deduct"):
            return
    trace_hint = ""
    if isinstance(meta, dict):
        tid = meta.get("trace_id")
        if tid:
            trace_hint = str(tid)[:128]
    logger.info(
        "[sutui-audit] db credit_ledger trace_id=%s user_id=%s entry_type=%s delta=%s balance_after=%s "
        "ref_type=%s ref_id=%s description=%s meta=%s",
        trace_hint or "-",
        user_id,
        et,
        delta,
        balance_after,
        (ref_type or "")[:32] or "-",
        (ref_id or "")[:128] or "-",
        (description or "")[:300],
        _json_clip(meta, 8000) if meta else "-",
    )


def log_capability_call_log_persisted(
    *,
    log_id: int,
    user_id: int,
    capability_id: str,
    credits_charged: Any,
    success: bool,
    source: str = "",
    request_summary: Any = None,
    error_message: Optional[str] = None,
) -> None:
    logger.info(
        "[sutui-audit] db capability_call_log id=%s user_id=%s capability_id=%s credits_charged=%s "
        "success=%s source=%s err=%s request=%s",
        log_id,
        user_id,
        capability_id,
        credits_charged,
        success,
        (source or "-")[:64],
        (error_message or "-")[:500],
        _json_clip(request_summary, 6000) if request_summary is not None else "-",
    )
