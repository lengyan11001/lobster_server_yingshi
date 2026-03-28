"""龙虾积分：统一为最多 4 位小数（与速推 x_billing 等返回的小数消耗对齐）。"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Union

QUANT = Decimal("0.0001")


def to_decimal(x: Any) -> Decimal:
    if x is None:
        return Decimal(0)
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(0)


def quantize_credits(x: Any) -> Decimal:
    d = to_decimal(x)
    if d < 0:
        d = Decimal(0)
    return d.quantize(QUANT, rounding=ROUND_HALF_UP)


def credits_json_float(x: Any) -> float:
    """API JSON 输出用 float（保留至多 4 位小数语义）。"""
    return float(quantize_credits(x))


def user_balance_decimal(user: Any) -> Decimal:
    return quantize_credits(getattr(user, "credits", None) or 0)
