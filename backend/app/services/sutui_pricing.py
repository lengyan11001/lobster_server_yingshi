"""速推/xskill：从官方 docs 接口读取模型定价并估算预扣积分（与 model-pricing-guide.md 一致）。"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import httpx

from ..core.config import settings

_DOCS_CACHE: Dict[str, Tuple[float, Optional[dict]]] = {}
_CACHE_TTL_SEC = 3600


def _api_base() -> str:
    return (getattr(settings, "sutui_api_base", None) or "https://api.xskill.ai").rstrip("/")


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
        return int(math.ceil(float(d) * float(base)))

    if price_type == "fixed":
        return base

    if price_type == "token_based":
        return base

    if price_type == "audio_duration_based":
        d = _duration_seconds_from_params(params)
        if d <= 0:
            return base
        return int(math.ceil(d * float(base)))

    return base


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
