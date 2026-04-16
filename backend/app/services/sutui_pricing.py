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

# 公开 docs 无条目的对话模型：流式常无 x_billing，仅能按 usage×费率估算；费率按与非流式 x_billing 同量级校准，可用 env JSON 覆盖。
_BUILTIN_CHAT_USAGE_CREDITS_PER_1K_BY_MODEL: Dict[str, float] = {
    "deepseek-chat": 0.2,
}

# ---------------------------------------------------------------------------
# DeepSeek 官方定价（1 元 = 100 积分）
# https://api-docs.deepseek.com/quick_start/pricing/
# ---------------------------------------------------------------------------
_DEEPSEEK_OFFICIAL_CREDITS_PER_1M: Dict[str, Dict[str, float]] = {
    "deepseek-chat": {
        "input_cache_miss": 200.0,   # ¥2.0/1M → 200 credits/1M
        "input_cache_hit":   20.0,   # ¥0.2/1M →  20 credits/1M
        "output":           300.0,   # ¥3.0/1M → 300 credits/1M
    },
    "deepseek-reasoner": {
        "input_cache_miss": 400.0,   # ¥4.0/1M
        "input_cache_hit":  100.0,   # ¥1.0/1M
        "output":          1600.0,   # ¥16.0/1M
    },
}


def credits_from_direct_api_usage(model: str, usage: Optional[dict]) -> Decimal:
    """按 DeepSeek 官方定价 + usage 中 cache hit/miss 精确计费。1 元 = 100 积分。"""
    if not usage or not isinstance(usage, dict):
        return Decimal(0)
    mid = (model or "").strip()
    pricing = _DEEPSEEK_OFFICIAL_CREDITS_PER_1M.get(mid)
    if not pricing:
        return Decimal(0)

    cache_hit = 0
    cache_miss = 0
    try:
        cache_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    except (TypeError, ValueError):
        pass
    try:
        cache_miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    except (TypeError, ValueError):
        pass
    if cache_hit == 0 and cache_miss == 0:
        try:
            cache_miss = int(usage.get("prompt_tokens") or 0)
        except (TypeError, ValueError):
            pass

    completion = 0
    try:
        completion = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        pass

    cost = (
        cache_hit * pricing["input_cache_hit"] / 1_000_000
        + cache_miss * pricing["input_cache_miss"] / 1_000_000
        + completion * pricing["output"] / 1_000_000
    )
    if cost <= 0:
        return Decimal(0)
    return quantize_credits(cost)


def _usage_credits_per_1k_for_model(model_id: str) -> float:
    """无 docs、无上游价字段时：先查内置/配置的按模型费率，否则 sutui_chat_fallback_credits_per_1k。"""
    mid = (model_id or "").strip()
    try:
        default_rate = float(getattr(settings, "sutui_chat_fallback_credits_per_1k", 0.0) or 0.0)
    except (TypeError, ValueError):
        default_rate = 0.0
    merged: Dict[str, float] = dict(_BUILTIN_CHAT_USAGE_CREDITS_PER_1K_BY_MODEL)
    raw = (getattr(settings, "sutui_chat_usage_credits_per_1k_by_model_json", None) or "").strip()
    if raw:
        try:
            extra = json.loads(raw)
        except json.JSONDecodeError:
            extra = None
        if isinstance(extra, dict):
            for k, v in extra.items():
                ks = str(k).strip()
                if not ks:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if fv > 0:
                    merged[ks] = fv
    if mid and mid in merged:
        return merged[mid]
    return default_rate


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


_MODEL_SHORT_TO_FULL: Dict[str, str] = {
    "flux-2": "fal-ai/flux-2/flash",
    "flux-2/flash": "fal-ai/flux-2/flash",
    "seedream": "fal-ai/bytedance/seedream/v4.5/text-to-image",
    "seedream-4.5": "fal-ai/bytedance/seedream/v4.5/text-to-image",
    "seedream-5": "fal-ai/bytedance/seedream/v5/lite/text-to-image",
    "nano-banana-pro": "fal-ai/nano-banana-pro",
    "nano-banana-2": "fal-ai/nano-banana-2",
    "sora-2": "fal-ai/sora-2/text-to-video",
    "gemini": "kapon/gemini-3-pro-image-preview",
    "qwen-image-edit": "fal-ai/qwen-image-edit-2511-multiple-angles",
}


_LEGACY_PREFIX_REWRITE: Tuple[Tuple[str, str], ...] = (
    ("sora2pub/", "fal-ai/sora-2/"),
    ("sprcra/sora-2-vip/", "fal-ai/sora-2/vip/"),
)


def _resolve_model_alias(mid: str) -> str:
    """Map short/friendly model names to full SuTui model IDs for pricing lookups."""
    if mid in _MODEL_SHORT_TO_FULL:
        return _MODEL_SHORT_TO_FULL[mid]
    low = mid.lower()
    for short, full in _MODEL_SHORT_TO_FULL.items():
        if low == short.lower():
            return full
    for old_prefix, new_prefix in _LEGACY_PREFIX_REWRITE:
        if low.startswith(old_prefix):
            return new_prefix + mid[len(old_prefix):]
    return mid


def fetch_model_docs_data(model_id: str) -> Optional[dict]:
    """GET /api/v3/models/{model_id}/docs 返回的 data 对象（含 pricing）。"""
    if not model_id or not str(model_id).strip():
        return None
    mid = _resolve_model_alias(str(model_id).strip())
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

    # per_second / dynamic_per_second: base_price 可能为 None，用 per_second 字段
    if price_type in ("per_second", "dynamic_per_second"):
        try:
            rate = float(pricing.get("per_second") or 0)
        except (TypeError, ValueError):
            rate = 0.0
        if rate <= 0 and base > 0:
            rate = float(base)
        if rate <= 0:
            return 0
        d = _duration_seconds_from_params(params)
        if d <= 0:
            d = 5.0
        return _quantize_credits(math.ceil(d * rate))

    # duration_map: base_price 是最短时长的价格，按时长比例估算
    if price_type == "duration_map":
        if base <= 0:
            return 0
        d = _duration_seconds_from_params(params)
        if d <= 0:
            return base
        examples = pricing.get("examples") or []
        for ex in examples:
            desc = str(ex.get("description") or "")
            try:
                ex_dur = float("".join(c for c in desc if c.isdigit() or c == "."))
            except (ValueError, TypeError):
                continue
            if ex_dur > 0 and d <= ex_dur:
                return int(ex.get("price", base))
        if examples:
            return int(examples[-1].get("price", base))
        return base

    # token_postcharge: 后付费，预扣用 examples 中最低价作保守估计
    if price_type == "token_postcharge":
        examples = pricing.get("examples") or []
        if examples:
            prices = [int(ex.get("price", 0)) for ex in examples if ex.get("price")]
            if prices:
                return min(prices)
        return max(base, 100)

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

    if price_type in ("duration_based", "duration_price"):
        d = _duration_seconds_from_params(params)
        if d <= 0:
            d = 5.0
        return _quantize_credits(float(math.ceil(float(d) * float(base))))

    if price_type == "fixed":
        return base

    if price_type == "matrix":
        d = _duration_seconds_from_params(params)
        if d > 0:
            return _quantize_credits(float(math.ceil(d * float(base))))
        return base

    if price_type == "token_based":
        pt = int(params.get("prompt_tokens", 0) or 0)
        ct = int(params.get("completion_tokens", 0) or 0)
        total = pt + ct
        if total > 0:
            units = math.ceil(total / 1000.0)
            raw = units * float(base)
            return _quantize_credits(raw)
        return _quantize_credits(float(base))

    if price_type in ("audio_duration_based", "audio_duration"):
        d = _duration_seconds_from_params(params)
        if d <= 0:
            return _quantize_credits(float(base))
        return _quantize_credits(float(math.ceil(d * float(base))))

    if price_type == "char_based":
        char_count = 0
        prompt = params.get("prompt") or params.get("text") or ""
        if isinstance(prompt, str):
            char_count = len(prompt)
        if char_count <= 0:
            char_count = 100
        units = math.ceil(char_count / 1000.0)
        return _quantize_credits(float(units * base))

    if price_type in ("resolution_quantity", "size_based"):
        n = params.get("num_images") or params.get("n") or params.get("batch_size") or 1
        try:
            n_int = int(n)
        except (TypeError, ValueError):
            n_int = 1
        if n_int < 1:
            n_int = 1
        return base * n_int

    return _quantize_credits(float(base))


def credits_from_chat_usage_when_no_docs_pricing(
    usage: Optional[dict], model_id: Optional[str] = None
) -> Decimal:
    """
    docs 无定价或定价无法用于本次扣费时：按上游 chat/completions 返回的 usage 事后折算积分。
    model_id 用于按模型费率（内置表或 SUTUI_CHAT_USAGE_CREDITS_PER_1K_BY_MODEL_JSON）；预检阶段仍可不拦截。
    """
    rate = _usage_credits_per_1k_for_model(model_id or "")
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
