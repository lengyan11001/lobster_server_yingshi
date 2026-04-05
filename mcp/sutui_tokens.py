"""服务器侧速推 Token：仅 bihuo / yingshi 两池；无品牌或非这两类不提供 Token（无 USER 兜底）。

环境变量：
- 必火：SUTUI_SERVER_TOKENS_BIHUO、SUTUI_SERVER_TOKEN_BIHUO
- 影视：SUTUI_SERVER_TOKENS_YINGSHI、SUTUI_SERVER_TOKEN_YINGSHI
- 兼容（仅站内 LLM 探测等 internal 路径）：SUTUI_SERVER_TOKENS、SUTUI_SERVER_TOKEN、sutui_config.json

不再读取 SUTUI_SERVER_TOKENS_USER（无品牌用户不允许走速推）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_sutui_token_lock = asyncio.Lock()
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


def get_sutui_tokens_list_bihuo() -> List[str]:
    return _parse_pool("SUTUI_SERVER_TOKENS_BIHUO", "SUTUI_SERVER_TOKEN_BIHUO")


def get_sutui_tokens_list_yingshi() -> List[str]:
    return _parse_pool("SUTUI_SERVER_TOKENS_YINGSHI", "SUTUI_SERVER_TOKEN_YINGSHI")


def sutui_token_ref_from_secret(token: Optional[str]) -> str:
    """对账用：完整 sk 的短 SHA256 前缀（不可逆），勿写入日志明文。"""
    t = (token or "").strip()
    if not t:
        return ""
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:12]


def sutui_token_recon_meta(token: Optional[str], pool_key: str) -> Dict[str, Any]:
    """写入 credit_ledger.meta['_recon']；仅站内对账，勿对用户端展示。"""
    ref = sutui_token_ref_from_secret(token)
    pk = (pool_key or "").strip() or "unknown"
    if not ref:
        return {}
    return {"_recon": {"sutui_pool": pk, "sutui_token_ref": ref}}


def _tokens_and_pool_key_user(*, brand_mark: Optional[str]) -> Tuple[str, List[str]]:
    """终端用户：仅 bihuo / yingshi；池为空则无 Token，不使用 USER/legacy 兜底。"""
    b = (brand_mark or "").strip().lower()
    if b == "bihuo":
        return "bihuo", get_sutui_tokens_list_bihuo()
    if b == "yingshi":
        return "yingshi", get_sutui_tokens_list_yingshi()
    return "none", []


def _internal_probe_pool_and_list() -> Tuple[str, List[str]]:
    """站内探测：优先 bihuo → yingshi → legacy；返回 (池名, token 列表)。"""
    for pk, lst in (
        ("bihuo", get_sutui_tokens_list_bihuo()),
        ("yingshi", get_sutui_tokens_list_yingshi()),
        ("legacy", _legacy_sutui_tokens_list()),
    ):
        if lst:
            return pk, lst
    return "none", []


def _internal_probe_token_list() -> List[str]:
    """兼容旧调用方：仅返回第一个非空列表。"""
    _, lst = _internal_probe_pool_and_list()
    return lst


async def next_sutui_server_token_with_pool(*, brand_mark: Optional[str] = None) -> Tuple[Optional[str], str]:
    """终端请求：返回 (token, 池名)；无 token 时第二个值为逻辑池名（none/bihuo/yingshi）。"""
    pool_key, lst = _tokens_and_pool_key_user(brand_mark=brand_mark)
    if not lst:
        return None, pool_key
    async with _sutui_token_lock:
        idx = _sutui_pool_index.get(pool_key, 0) % len(lst)
        _sutui_pool_index[pool_key] = idx + 1
        return lst[idx], pool_key


async def next_sutui_server_token(*, brand_mark: Optional[str] = None) -> Optional[str]:
    """终端请求：brand_mark 必须为 bihuo/yingshi 且对应池已配置。"""
    t, _ = await next_sutui_server_token_with_pool(brand_mark=brand_mark)
    return t


async def next_sutui_server_token_internal_with_pool() -> Tuple[Optional[str], str]:
    """站内 LLM 列表/探测：返回 (token, bihuo|yingshi|legacy|none)。"""
    picked_key, lst = _internal_probe_pool_and_list()
    if not lst:
        return None, picked_key
    lock_key = f"internal::{picked_key}"
    async with _sutui_token_lock:
        idx = _sutui_pool_index.get(lock_key, 0) % len(lst)
        _sutui_pool_index[lock_key] = idx + 1
        return lst[idx], picked_key


async def next_sutui_server_token_internal() -> Optional[str]:
    """站内 LLM 列表/探测：不绑定终端用户品牌，仅从已配置的品牌池或 legacy 取 Token。"""
    t, _ = await next_sutui_server_token_internal_with_pool()
    return t
