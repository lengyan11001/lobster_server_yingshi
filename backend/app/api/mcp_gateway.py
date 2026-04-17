"""MCP 代理：Gateway 调用本端点时代入当前用户的 JWT，再转发到真实 MCP（8001）。

- 智能对话时 chat 接口会按 agent_id 将用户 token 写入缓存（TTL 10 分钟）。
- OpenClaw Gateway 应配置 MCP URL 为本代理（如 http://127.0.0.1:8000/mcp-gateway）。
- 代理收到请求时优先从 Header（x-user-authorization / Authorization）取 token（若 Gateway 透传），
  否则从缓存按 x-openclaw-agent-id 取 token，再转发到 MCP 并注入 Authorization。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)
router = APIRouter()

# 默认转发到同机 MCP 服务
MCP_BACKEND_URL = os.environ.get("AI_TEST_PLATFORM_MCP_GATEWAY_BACKEND_URL", "http://127.0.0.1:8001/mcp").rstrip("/")
MCP_TOKEN_TTL_SECONDS = int(os.environ.get("MCP_GATEWAY_TOKEN_TTL_SECONDS", "600"))

# 转发 MCP 后端时禁止把浏览器整包 Cookie / 各类代理头带上去游，易触发边缘 nginx 400（典型 ~150B HTML）或无谓泄露。
_UPSTREAM_HEADER_ALLOWLIST = frozenset(
    {
        "content-type",
        "accept",
        "accept-language",
        "accept-encoding",
        "user-agent",
        "authorization",
        "x-user-authorization",
        "x-openclaw-agent-id",
        "x-installation-id",
    }
)


def _headers_for_upstream(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _UPSTREAM_HEADER_ALLOWLIST and v is not None:
            out[k] = v
    return out


# agent_id -> (token, expiry_ts)
_mcp_token_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def set_mcp_token_for_agent(agent_id: str, token: str, ttl_seconds: int = MCP_TOKEN_TTL_SECONDS) -> None:
    """在发起智能对话前调用，将当前用户的 token 按 agent_id 写入缓存，供后续 MCP 代理使用。"""
    if not agent_id or not token:
        return
    expiry = time.time() + ttl_seconds
    with _cache_lock:
        _mcp_token_cache[agent_id] = (token.strip(), expiry)


def _mcp_gateway_forward_read_timeout_sec(body: bytes) -> float:
    """tools/call invoke_capability 与 lobster_online chat._exec_tool / mcp/http_server 对齐，避免长视频被 120s 掐断。"""
    try:
        jb = json.loads(body)
    except Exception:
        return 120.0
    if not isinstance(jb, dict) or jb.get("method") != "tools/call":
        return 120.0
    params = jb.get("params")
    if not isinstance(params, dict):
        return 120.0
    if (params.get("name") or "").strip() != "invoke_capability":
        return 120.0
    args = params.get("arguments")
    if not isinstance(args, dict):
        return 120.0
    cap = (args.get("capability_id") or "").strip()
    if cap == "video.generate":
        return 40 * 60.0
    if cap == "task.get_result":
        return 35 * 60.0
    if cap == "image.generate":
        return 25 * 60.0
    if cap == "comfly.veo.daihuo_pipeline":
        return 130 * 60.0
    if cap == "comfly.veo":
        return 40 * 60.0
    return 120.0


def get_mcp_token_from_request(request: Request) -> Optional[str]:
    """从代理收到的请求中解析用户 token：Header 优先，agent_id 缓存次之，最近缓存兜底。

    mcp-remote (stdio→HTTP bridge) 不会转发应用层 Header，因此当 OpenClaw 通过
    mcp-remote 调用本代理时，Header 中不会有 token 也不会有 agent_id。
    兜底策略：取缓存中最近写入（expiry 最大）的 token，因为 chat.py 在调用 OpenClaw
    前刚刚 set_mcp_token_for_agent()，时间差通常 < 1 秒。
    """
    # 1) 若 Gateway 透传了用户 JWT，直接使用
    auth = request.headers.get("x-user-authorization") or request.headers.get("Authorization") or ""
    if auth and "bearer" in auth.lower():
        token = auth.split(" ", 1)[-1].strip() if " " in auth else auth.strip()
        if token:
            return token
    # 2) 按 agent_id 从缓存取（Gateway 透传 x-openclaw-agent-id 时生效）
    agent_id = (request.headers.get("x-openclaw-agent-id") or "").strip()
    if agent_id:
        with _cache_lock:
            entry = _mcp_token_cache.get(agent_id)
        if entry:
            token, expiry = entry
            if time.time() < expiry and token:
                return token
            with _cache_lock:
                _mcp_token_cache.pop(agent_id, None)
    # 3) mcp-remote 不传 Header：取缓存中最近写入且未过期的 token（兜底）
    now = time.time()
    with _cache_lock:
        best_token: Optional[str] = None
        best_expiry = 0.0
        stale_keys: list[str] = []
        for k, (t, exp) in _mcp_token_cache.items():
            if exp <= now:
                stale_keys.append(k)
                continue
            if exp > best_expiry:
                best_expiry = exp
                best_token = t
        for k in stale_keys:
            _mcp_token_cache.pop(k, None)
    if best_token:
        logger.debug("mcp_gateway: using most-recent cached token (no agent_id in headers)")
        return best_token
    return None


@router.post("/mcp-gateway", include_in_schema=False)
async def mcp_gateway_proxy(request: Request) -> Response:
    """将 Gateway 的 MCP 请求转发到真实 MCP，并注入当前用户 token（若有）。"""
    try:
        body = await request.body()
    except Exception as e:
        logger.warning("mcp_gateway read body error: %s", e)
        return Response(content=b"", status_code=400)
    token = get_mcp_token_from_request(request)
    headers = _headers_for_upstream(request)
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["x-user-authorization"] = f"Bearer {token}"
    # 安装槽：显式透传，避免个别 ASGI/代理层对键名处理差异导致丢失
    xi = (request.headers.get("X-Installation-Id") or request.headers.get("x-installation-id") or "").strip()
    if xi:
        headers["X-Installation-Id"] = xi
    try:
        _read_sec = _mcp_gateway_forward_read_timeout_sec(body)
        _gw_timeout = httpx.Timeout(connect=45.0, read=_read_sec, write=600.0, pool=60.0)
        async with httpx.AsyncClient(timeout=_gw_timeout, trust_env=False) as client:
            r = await client.post(MCP_BACKEND_URL, content=body, headers=headers)
        # 只透传对 JSON-RPC 有用的响应头，避免 hop-by-hop 等干扰
        out_headers = {}
        for name in ("content-type", "content-length"):
            if name in r.headers:
                out_headers[name] = r.headers[name]
        return Response(
            content=r.content,
            status_code=r.status_code,
            headers=out_headers,
        )
    except Exception as e:
        logger.exception("mcp_gateway forward error: %s", e)
        return Response(content=b"", status_code=502)
