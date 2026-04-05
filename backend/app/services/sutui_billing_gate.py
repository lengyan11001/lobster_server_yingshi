"""速推调用前统一预检：只认官方 docs 定价表（见 docs/model-pricing-guide.md）估算龙虾积分；无表/估不出则禁止调用上游。

素材生成走 /capabilities/pre-deduct；LLM chat 复用同一定价逻辑。
实际扣费仍以速推响应为准，由 sutui_chat 与 record_call 等路径记流水。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import User
from .credits_amount import quantize_credits, user_balance_decimal
from .sutui_pricing import estimate_credits_from_pricing, fetch_model_pricing


def assert_pricing_pre_deduct_allows_upstream_or_http(
    db: Session,
    user: User,
    model_id: str,
    params: Optional[Dict[str, Any]],
    *,
    action_label: str = "本次调用",
) -> Decimal:
    """
    仅用定价表做「事前预估」：余额不足 402；无定价或无法用本次参数估算 400。
    不调用速推；返回量化后的预估积分（便于日志）。
    """
    mid = (model_id or "").strip()
    if not mid:
        raise HTTPException(status_code=400, detail="缺少 model，无法按速推定价表预检。")

    pricing = fetch_model_pricing(mid)
    if not pricing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"模型 `{mid}` 在速推侧无可用定价表（docs），无法预估积分，{action_label}已中止。"
                "请核对 model 是否正确或联系管理员。"
            ),
        )

    est = estimate_credits_from_pricing(pricing, params or {})
    if est <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"模型 `{mid}` 虽有定价表，但无法按本次参数得到正的预扣估算（得到 {est}）。"
                "请检查参数或联系管理员。"
            ),
        )

    need = quantize_credits(est)
    db.refresh(user)
    bal = user_balance_decimal(user)
    if bal < need:
        raise HTTPException(
            status_code=402,
            detail=(
                f"积分不足：按速推定价表本次预估至少 {need} 积分，当前余额 {bal}。"
                "请充值或缩短上下文/降低 max_tokens 后重试。"
            ),
        )
    return need
