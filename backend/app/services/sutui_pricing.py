"""速推/xskill：从官方 docs 拉取 pricing 并估算预扣/结算积分。

接口与字段语义与仓库文档一致：docs/model-pricing-guide.md
（GET /api/v3/models/{model_id}/docs，pricing.price_type / base_price 等）。
"""
from __future__ import annotations

import json
import math
import time
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import httpx

from ..core.config import settings
from .credits_amount import quantize_credits

_DOCS_CACHE: Dict[str, Tuple[float, Optional[dict]]] = {}
_CACHE_TTL_SEC = 3600


def _api_base() -> str:
    return (getattr(settings, "sutui_api_base", None) or "https://api.xskill.ai").rstrip("/")


def _quantize_credits(value: float) -> int:
    """与速推侧金额习惯一致：先保留两位小数再取整为积分（避免浮点误差）。"""
    return int(round(float(value) + 1e-9, 2))


def _duration_seconds_from_params(params: Dict[str, Any]) -> float:
    for key in ("duration", "duration_seconds", "length", "video_length", "audio_length"):
        v = params.get(key)
        if v is None:
            continue
        try:
            d = float(v)
            if d > 0:
                return d
        except (TypeError, ValueError):
            continue
    return 0.0


def fetch_model_docs_data(model_id: str) -> Optional[dict]:
    """GET /api/v3/models/{model_id}/docs 返回的 data 对象（含 pricing）。"""
    if not model_id or not str(model_id).strip():
        return None
    mid = str(model_id).strip()
    now = time.time()
    if mid in _DOCS_CACHE:
        ts, data = _DOCS_CACHE[mid]
        if now - ts < _CACHE_TTL_SEC and data is not None:
            return data
    safe = quote(mid, safe="")
    url = f"{_api_base()}/api/v3/models/{safe}/docs"
    try:
        r = httpx.get(url, params={"lang": "zh"}, timeout=20.0)
        if r.status_code == 404:
            _DOCS_CACHE[mid] = (now, None)
            return None
        r.raise_for_status()
        j = r.json()
        if not isinstance(j, dict) or int(j.get("code", 0)) != 200:
            _DOCS_CACHE[mid] = (now, None)
            return None
        data = j.get("data")
        if not isinstance(data, dict):
            _DOCS_CACHE[mid] = (now, None)
            return None
        _DOCS_CACHE[mid] = (now, data)
        return data
    except Exception:
        _DOCS_CACHE[mid] = (now, None)
        return None


def fetch_model_pricing(model_id: str) -> Optional[dict]:
    data = fetch_model_docs_data(model_id)
    if not data:
        return None
    p = data.get("pricing")
    return p if isinstance(p, dict) else None


def estimate_credits_from_pricing(pricing: dict, params: Optional[dict]) -> int:
    """根据 pricing + 请求参数估算预扣积分（保守估计，避免低估）。"""
    params = params or {}
    if not pricing:
        return 0
    price_type = (pricing.get("price_type") or "").strip().lower()
    try:
        base = int(pricing.get("base_price") or 0)
    except (TypeError, ValueError):
        base = 0
    if base <= 0:
        return 0

    if price_type == "quantity_based":
        n = params.get("num_images") or params.get("n") or params.get("batch_size") or 1
        try:
            n_int = int(n)
        except (TypeError, ValueError):
            n_int = 1
        if n_int < 1:
            n_int = 1
        return base * n_int

    if price_type == "duration_based":
        d = _duration_seconds_from_params(params)
        if d <= 0:
            d = 5.0
        return _quantize_credits(float(math.ceil(float(d) * float(base))))

    if price_type == "fixed":
        return base

    if price_type == "token_based":
        pt = int(params.get("prompt_tokens", 0) or 0)
        ct = int(params.get("completion_tokens", 0) or 0)
        total = pt + ct
        if total > 0:
            # base_price 按「每千 token」计（与速推 docs 常见约定一致）
            units = math.ceil(total / 1000.0)
            raw = units * float(base)
            return _quantize_credits(raw)
        return _quantize_credits(float(base))

    if price_type == "audio_duration_based":
        d = _duration_seconds_from_params(params)
        if d <= 0:
            return _quantize_credits(float(base))
        return _quantize_credits(float(math.ceil(d * float(base))))

    return _quantize_credits(float(base))


def credits_from_chat_usage_when_no_docs_pricing(usage: Optional[dict]) -> Decimal:
    """
    docs 无定价或定价无法用于本次扣费时：按上游 chat/completions 返回的 usage 事后折算积分。
    与 SUTUI_CHAT_MODEL_MAP 等无关，只看 token 计数；预检阶段仍可不拦截（无 pricing 时 _require_balance_before_upstream_chat 直接 return）。
    """
    try:
        rate = float(getattr(settings, "sutui_chat_fallback_credits_per_1k", 0.0) or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    if rate <= 0:
        return Decimal(0)
    if not usage or not isinstance(usage, dict):
        return Decimal(0)
    total = 0
    tt = usage.get("total_tokens")
    if tt is not None:
        try:
            total = int(tt)
        except (TypeError, ValueError):
            total = 0
    if total <= 0:
        try:
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            total = pt + ct
        except (TypeError, ValueError):
            total = 0
    if total <= 0:
        return Decimal(0)
    units = math.ceil(total / 1000.0)
    return quantize_credits(units * rate)


def estimate_pre_deduct_credits(model_id: str, params: Optional[dict]) -> Tuple[int, Optional[str]]:
    """
    返回 (预扣积分, 错误文案)。错误非空表示不允许调用（无定价或无法估算）。
    """
    pricing = fetch_model_pricing(model_id)
    if not pricing:
        return 0, "该模型无法在速推获取定价（docs 无 pricing 或未开放），请联系管理员配置。"
    est = estimate_credits_from_pricing(pricing, params)
    if est <= 0:
        return 0, "该模型定价无效，请联系管理员配置。"
    return est, None


def _dict_looks_like_account_balance(d: dict) -> bool:
    """含余额语义时，避免把字段名 credits 误当作「本次消耗」（与 mcp/http_server 一致）。"""
    kl = {str(k).lower() for k in d}
    return bool(kl & {"balance", "remaining", "remaining_credits", "total_balance", "available", "points"})


def upstream_numeric_credits_to_decimal(v: Any) -> Decimal:
    """速推常在 x_billing 等字段返回小数积分（如 0.9558）；统一量化为 4 位小数。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return Decimal(0)
    if x <= 0:
        return Decimal(0)
    return quantize_credits(x)


def _coerce_positive_credit_number(v: Any) -> Optional[Decimal]:
    """上游可能返回 float / int / 数字字符串；仅接受正数。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if v <= 0:
            return None
        return upstream_numeric_credits_to_decimal(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            x = float(s)
        except ValueError:
            return None
        if x <= 0:
            return None
        return upstream_numeric_credits_to_decimal(x)
    return None


def extract_upstream_billing_snapshot(data: Optional[dict]) -> dict[str, Any]:
    """
    从 chat/completions 等响应中抽出与计费相关的字段，便于日志对照（含 x_billing、usage 等）。
    """
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    priority = ("x_billing", "X-Billing", "billing", "usage", "service_tier")
    for k in priority:
        if k in data:
            out[k] = data[k]
    for k, v in data.items():
        if k in out:
            continue
        lk = str(k).lower()
        if any(
            x in lk
            for x in (
                "credit",
                "price",
                "cost",
                "bill",
                "charge",
                "usage",
                "x_billing",
                "sutui",
            )
        ):
            out[k] = v
    return out


_SKIP_UPSTREAM_CREDIT_RECURSE_KEYS = frozenset(
    {
        "choices",
        "messages",
        # 已在下面优先分支单独处理，避免在总遍历里与其它字段取 max 混算
        "x_billing",
        "x-billing",
    }
)


def extract_upstream_reported_credits(obj: Any, _depth: int = 0) -> Decimal:
    """
    从速推 chat/completions 或任务类完整 JSON 中解析「本次消耗积分」（4 位小数）。
    与 mcp/http_server 计费解析字段集合对齐；优先于 docs 定价推算。

    注意：不得遍历 OpenAI 样式的 ``choices``：助手/工具正文中常含 JSON，字段名 cost/price
    可能是套餐价、内部参数等，若与全树 max 合并会把「本次消耗」抬到荒谬整数（如 25）。
    速推官方账单一般以顶层 ``x_billing``（或 ``X-Billing``）为准，优先只信该子树。
    """
    if _depth > 42:
        return Decimal(0)
    if isinstance(obj, dict):
        xb = obj.get("x_billing")
        if xb is None:
            xb = obj.get("X-Billing")
        if xb is not None:
            if isinstance(xb, str):
                xs = xb.strip()
                if xs.startswith("{"):
                    try:
                        xb = json.loads(xs)
                    except Exception:
                        xb = None
                else:
                    pnum = _coerce_positive_credit_number(xs)
                    if pnum is not None and pnum > 0:
                        return pnum
                    xb = None
            if xb is not None:
                sub = extract_upstream_reported_credits(xb, _depth + 1)
                if sub > 0:
                    return sub

    best = Decimal(0)
    if isinstance(obj, dict):
        balance_shape = _dict_looks_like_account_balance(obj)
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in _SKIP_UPSTREAM_CREDIT_RECURSE_KEYS:
                continue
            if lk in (
                "credits_used",
                "credits_charged",
                "credit_cost",
                "consumed_credits",
                "usage_credits",
                "cost",
                "price",
            ):
                parsed = _coerce_positive_credit_number(v)
                if parsed is not None:
                    best = max(best, parsed)
            elif lk == "credits" and not balance_shape:
                parsed = _coerce_positive_credit_number(v)
                if parsed is not None:
                    best = max(best, parsed)
            elif isinstance(v, (dict, list)):
                best = max(best, extract_upstream_reported_credits(v, _depth + 1))
            elif isinstance(v, str):
                s = v.strip()
                if s.startswith("{"):
                    try:
                        best = max(best, extract_upstream_reported_credits(json.loads(s), _depth + 1))
                    except Exception:
                        pass
    elif isinstance(obj, list):
        for it in obj:
            best = max(best, extract_upstream_reported_credits(it, _depth + 1))
    return best
