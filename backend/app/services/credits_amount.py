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


def quantize_credits_signed(x: Any) -> Decimal:
    """流水变动量：可正可负，不得将负数钳为 0（与余额 quantize_credits 区分）。"""
    return to_decimal(x).quantize(QUANT, rounding=ROUND_HALF_UP)


def credits_json_float(x: Any) -> float:
    """API JSON 输出用 float（保留至多 4 位小数语义）。"""
    return float(quantize_credits(x))


def credits_json_float_signed(x: Any) -> float:
    """流水变动 JSON：可正可负。"""
    return float(quantize_credits_signed(x))


def user_balance_decimal(user: Any) -> Decimal:
    return quantize_credits(getattr(user, "credits", None) or 0)


def ledger_display_delta(row: Any) -> Decimal:
    """
    展示用「本行变动积分」：优先 ORM 的 delta。
    若 SQLite INTEGER 列把小数扣费写成 0，则使用 meta.deduct_credits（sutui_chat 等写入）还原为负数。
    """
    d = quantize_credits_signed(getattr(row, "delta", None) or 0)
    if d != 0:
        return d
    meta = getattr(row, "meta", None)
    if not isinstance(meta, dict):
        return d
    dc = meta.get("deduct_credits")
    if dc is None:
        return d
    return quantize_credits_signed(-to_decimal(dc))
