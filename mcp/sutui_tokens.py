"""服务器侧速推 Token 池：按品牌（bihuo / yingshi）分流，无管理员单独池。

环境变量（显式池优先，否则回退到既有 SUTUI_SERVER_TOKENS / SUTUI_SERVER_TOKEN / sutui_config.json）：
- 北极火：SUTUI_SERVER_TOKENS_BIHUO、SUTUI_SERVER_TOKEN_BIHUO
- 影视：SUTUI_SERVER_TOKENS_YINGSHI、SUTUI_SERVER_TOKEN_YINGSHI
- 默认池（无品牌、其它品牌、或品牌池未配置时的兜底）：SUTUI_SERVER_TOKENS_USER、SUTUI_SERVER_TOKEN_USER
- 兼容：SUTUI_SERVER_TOKENS、SUTUI_SERVER_TOKEN、sutui_config.json
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import List, Optional, Tuple

_sutui_token_lock = asyncio.Lock()
# 轮询游标：按池键区分（bihuo / yingshi / user）
_sutui_pool_index: dict[str, int] = {}


def _load_sutui_token_from_file() -> str:
    try:
        p = Path(__file__).resolve().parent.parent / "sutui_config.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return (data.get("token") or "").strip()
    except Exception:
        pass
    return ""


def _legacy_sutui_tokens_list() -> List[str]:
    raw = os.environ.get("SUTUI_SERVER_TOKENS", "").strip()
    if raw:
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if tokens:
            return tokens
    single = os.environ.get("SUTUI_SERVER_TOKEN", "").strip()
    if single:
        return [single]
    from_file = _load_sutui_token_from_file()
    if from_file:
        return [from_file]
    return []


def _parse_pool(comma_key: str, single_key: str) -> List[str]:
    raw = os.environ.get(comma_key, "").strip()
    if raw:
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if tokens:
            return tokens
    single = os.environ.get(single_key, "").strip()
    if single:
        return [single]
    return []


def get_sutui_tokens_list_user() -> List[str]:
    u = _parse_pool("SUTUI_SERVER_TOKENS_USER", "SUTUI_SERVER_TOKEN_USER")
    if u:
        return u
    return _legacy_sutui_tokens_list()


def get_sutui_tokens_list_bihuo() -> List[str]:
    return _parse_pool("SUTUI_SERVER_TOKENS_BIHUO", "SUTUI_SERVER_TOKEN_BIHUO")


def get_sutui_tokens_list_yingshi() -> List[str]:
    return _parse_pool("SUTUI_SERVER_TOKENS_YINGSHI", "SUTUI_SERVER_TOKEN_YINGSHI")


def _user_fallback_tokens() -> List[str]:
    u = get_sutui_tokens_list_user()
    if u:
        return u
    return _legacy_sutui_tokens_list()


def _tokens_and_pool_key(*, brand_mark: Optional[str]) -> Tuple[str, List[str]]:
    b = (brand_mark or "").strip().lower()
    if b == "bihuo":
        lst = get_sutui_tokens_list_bihuo()
        if lst:
            return "bihuo", lst
        return "user", _user_fallback_tokens()
    if b == "yingshi":
        lst = get_sutui_tokens_list_yingshi()
        if lst:
            return "yingshi", lst
        return "user", _user_fallback_tokens()
    return "user", _user_fallback_tokens()


async def next_sutui_server_token(*, brand_mark: Optional[str] = None) -> Optional[str]:
    """从对应池中轮询取下一条 Token。传 brand_mark 与 JWT / users.brand_mark 一致；不传则走默认用户池。"""
    pool_key, lst = _tokens_and_pool_key(brand_mark=brand_mark)
    if not lst:
        return None
    async with _sutui_token_lock:
        idx = _sutui_pool_index.get(pool_key, 0) % len(lst)
        _sutui_pool_index[pool_key] = idx + 1
        return lst[idx]
