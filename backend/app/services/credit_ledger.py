"""积分流水：每次余额变动写入 credit_ledger，便于对账（预扣/结算/退款/充值等）。"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional, Union

from sqlalchemy.orm import Session

from ..models import CreditLedger
from .credits_amount import quantize_credits

logger = logging.getLogger(__name__)


def append_credit_ledger(
    db: Session,
    user_id: int,
    delta: Union[int, float, Decimal, str],
    entry_type: str,
    balance_after: Union[int, float, Decimal, str],
    *,
    description: str = "",
    ref_type: Optional[str] = None,
    ref_id: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> CreditLedger:
    """delta：正数为入账，负数为出账；balance_after 为变动后 users.credits 快照（最多 4 位小数）。"""
    d = quantize_credits(delta)
    bal = quantize_credits(balance_after)
    row = CreditLedger(
        user_id=user_id,
        delta=d,
        balance_after=bal,
        entry_type=(entry_type or "unknown")[:32],
        description=(description or "")[:512] or None,
        ref_type=(ref_type or "")[:32] or None,
        ref_id=(ref_id or "")[:128] or None,
        meta=meta,
    )
    db.add(row)
    return row
