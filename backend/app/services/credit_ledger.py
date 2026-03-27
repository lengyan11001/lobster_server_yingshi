"""积分流水：每次余额变动写入 credit_ledger，便于对账（预扣/结算/退款/充值等）。"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..models import CreditLedger

logger = logging.getLogger(__name__)


def append_credit_ledger(
    db: Session,
    user_id: int,
    delta: int,
    entry_type: str,
    balance_after: int,
    *,
    description: str = "",
    ref_type: Optional[str] = None,
    ref_id: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> CreditLedger:
    """delta：正数为入账，负数为出账；balance_after 为变动后 users.credits 快照。"""
    row = CreditLedger(
        user_id=user_id,
        delta=int(delta),
        balance_after=int(balance_after),
        entry_type=(entry_type or "unknown")[:32],
        description=(description or "")[:512] or None,
        ref_type=(ref_type or "")[:32] or None,
        ref_id=(ref_id or "")[:128] or None,
        meta=meta,
    )
    db.add(row)
    return row
