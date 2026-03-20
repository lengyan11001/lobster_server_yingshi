"""
HTTP MCP Server for 龙虾 (Lobster).
Simplified from ai_test_platform: no admin checks, dynamic catalog reload.
"""

import asyncio
import json
import logging
import os
from collections import OrderedDict
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route


BASE_URL = os.environ.get("AI_TEST_PLATFORM_BASE_URL", "http://localhost:8000").rstrip("/")
CAPABILITY_SUTUI_MCP_URL = os.environ.get("CAPABILITY_SUTUI_MCP_URL", "").strip()
CAPABILITY_UPSTREAM_URLS_JSON = os.environ.get("CAPABILITY_UPSTREAM_URLS_JSON", "").strip()


def _load_catalog_from_file(path: Path) -> Dict[str, Dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("catalog must be object")
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, dict):
            out[k] = v
    return out


def _load_capability_catalog() -> Dict[str, Dict[str, Any]]:
    """Reload catalog from files each time (hot-reload support)."""
    try:
        p_local = Path(__file__).resolve().parent / "capability_catalog.local.json"
        if p_local.exists():
            catalog = _load_catalog_from_file(p_local)
            p_base = Path(__file__).resolve().parent / "capability_catalog.json"
            if p_base.exists():
                base = _load_catalog_from_file(p_base)
                base.update(catalog)
                return base
            return catalog
    except Exception:
        pass
    try:
        p = Path(__file__).resolve().parent / "capability_catalog.json"
        if p.exists():
            return _load_catalog_from_file(p)
    except Exception:
        pass
    return {}


def _load_upstream_urls() -> Dict[str, str]:
    urls: Dict[str, str] = {}
    try:
        p = Path(__file__).resolve().parent.parent / "upstream_urls.json"
        if p.exists():
            parsed = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        urls[k.strip()] = v.strip()
    except Exception:
        pass
    if CAPABILITY_UPSTREAM_URLS_JSON:
        try:
            parsed = json.loads(CAPABILITY_UPSTREAM_URLS_JSON)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        urls[k.strip()] = v.strip()
        except Exception:
            pass
    if "sutui" not in urls and CAPABILITY_SUTUI_MCP_URL:
        urls["sutui"] = CAPABILITY_SUTUI_MCP_URL
    return urls


def _get_token_from_request(request: Request) -> Optional[str]:
    qp = request.query_params
    token = qp.get("token") or qp.get("api_key")
    if not token:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip() or None
    if not token:
        user_auth = request.headers.get("x-user-authorization") or ""
        if user_auth.lower().startswith("bearer "):
            token = user_auth[7:].strip() or None
    if not token:
        user_token = (request.headers.get("x-user-token") or "").strip()
        token = user_token or None
    return token or None


def _backend_headers(token: Optional[str]) -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _find_account_id_by_nickname(nickname: str, token: Optional[str]) -> Optional[int]:
    """Lookup account id by nickname from backend."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{BASE_URL}/api/accounts", headers=_backend_headers(token))
        if r.status_code == 200:
            for a in r.json().get("accounts", []):
                if a.get("nickname", "").strip() == nickname:
                    return a.get("id")
    except Exception:
        pass
    return None


def _tool_definitions(catalog: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    capability_list = sorted(catalog.keys())
    tools = [
        {
            "name": "list_capabilities",
            "description": "列出龙虾当前可用的全部能力",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "invoke_capability",
            "description": "调用龙虾能力（图片生成、视频解析、语音合成等）",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "capability_id": {
                        "type": "string",
                        "enum": capability_list,
                        "description": "能力 ID",
                    },
                    "payload": {
                        "type": "object",
                        "description": "能力调用参数",
                    },
                },
                "required": ["capability_id", "payload"],
            },
        },
        {
            "name": "manage_skills",
            "description": (
                "管理龙虾技能包：\n"
                "- list_store: 浏览本地技能商店\n"
                "- list_installed: 查看已安装技能\n"
                "- install: 安装商店中的技能包 (需 package_id)\n"
                "- uninstall: 卸载技能包 (需 package_id)\n"
                "- search_online: 搜索全球 MCP 在线技能库 (需 query，如 'image', 'database', 'search')\n"
                "- add_mcp: 添加 MCP 服务连接 (需 name + url)"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list_store", "list_installed", "install", "uninstall", "search_online", "add_mcp"],
                        "description": "操作类型",
                    },
                    "package_id": {
                        "type": "string",
                        "description": "技能包 ID（install/uninstall 时必填）",
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词（search_online 时使用，如 image, video, database, github）",
                    },
                    "name": {
                        "type": "string",
                        "description": "MCP 连接名称（add_mcp 时必填）",
                    },
                    "url": {
                        "type": "string",
                        "description": "MCP 服务地址（add_mcp 时必填）",
                    },
                },
                "required": ["action"],
            },
        },
        {
            "name": "save_asset",
            "description": "保存素材到本地（从URL下载图片/视频并存储，返回asset_id供后续引用和发布）",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "素材URL（图片或视频链接）"},
                    "media_type": {"type": "string", "enum": ["image", "video", "audio"], "description": "素材类型"},
                    "tags": {"type": "string", "description": "标签，逗号分隔"},
                    "prompt": {"type": "string", "description": "生成该素材时使用的提示词"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "list_assets",
            "description": "列出或搜索本地保存的素材（图片、视频等）",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "media_type": {"type": "string", "enum": ["image", "video", "audio"], "description": "按类型筛选"},
                    "query": {"type": "string", "description": "搜索关键词（匹配标签、提示词、文件名）"},
                    "limit": {"type": "integer", "description": "返回数量，默认20"},
                },
            },
        },
        {
            "name": "list_publish_accounts",
            "description": "列出已配置的发布账号（抖音、B站等平台）",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "open_account_browser",
            "description": "打开指定账号的浏览器窗口（会激活到最前面）。如果未登录会显示登录页。用于：用户要求打开某账号浏览器、发布前需要登录等场景。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_nickname": {"type": "string", "description": "账号昵称"},
                },
                "required": ["account_nickname"],
            },
        },
        {
            "name": "check_account_login",
            "description": "检查指定账号是否已在浏览器中登录（不会打开新窗口，仅检查已打开的浏览器）。用户说'登录完了'时调用此工具验证。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_nickname": {"type": "string", "description": "账号昵称"},
                },
                "required": ["account_nickname"],
            },
        },
        {
            "name": "publish_content",
            "description": "将素材发布到指定平台账号（如抖音、B站）。asset_id 可来自：save_asset 保存的素材，或 task.get_result 返回的 saved_assets[0].asset_id（速推生成的视频/图片）。发布流程全自动，由本工具完成，不要要求用户点加号或手动操作。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string", "description": "素材ID：来自 save_asset 或 task.get_result 返回的 saved_assets[0].asset_id（发布生成结果时必须用后者）"},
                    "account_nickname": {"type": "string", "description": "发布账号昵称"},
                    "title": {"type": "string", "description": "发布标题"},
                    "description": {"type": "string", "description": "发布描述/文案"},
                    "tags": {"type": "string", "description": "话题标签，逗号分隔"},
                    "cover_asset_id": {"type": "string", "description": "可选：封面素材ID（图片），部分平台支持单独设置封面"},
                    "options": {
                        "type": "object",
                        "description": (
                            "可选：平台发布参数（抖音 best-effort）。常用字段示例：\n"
                            "- visibility: public|friends|private\n"
                            "- schedule_publish: {enabled:true, datetime:\"YYYY-MM-DD HH:mm\"}\n"
                            "- location: \"深圳市南山区\"\n"
                            "- allow_comment / allow_duet / allow_stitch: true|false\n"
                            "- goods: {enabled:true, keyword:\"商品关键词\"}\n"
                        ),
                    },
                },
                "required": ["asset_id", "account_nickname"],
            },
        },
    ]
    return tools


def _redact_sensitive(value: Any) -> Any:
    # 不把 credits/credits_used 等整棵删掉：速推返回里含本次消耗，供计费解析；余额类仍用 balance/points 脱敏
    blocked_keys = {"api_key", "apikey", "token", "balance", "points", "account_id"}
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if str(k).lower() in blocked_keys:
                continue
            out[k] = _redact_sensitive(v)
        return out
    if isinstance(value, list):
        return [_redact_sensitive(x) for x in value]
    if isinstance(value, str):
        return re.sub(r"(sk-[A-Za-z0-9]{10,})", "[REDACTED]", value)
    return value


def _load_sutui_token() -> str:
    """Read the 速推 token from sutui_config.json."""
    try:
        p = Path(__file__).resolve().parent.parent / "sutui_config.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return (data.get("token") or "").strip()
    except Exception:
        pass
    return ""


def _get_sutui_tokens_list() -> List[str]:
    """服务器配置的速推算力 Token 列表：SUTUI_SERVER_TOKENS（逗号分隔）或 SUTUI_SERVER_TOKEN 单条，或 sutui_config.json。"""
    raw = os.environ.get("SUTUI_SERVER_TOKENS", "").strip()
    if raw:
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if tokens:
            return tokens
    single = os.environ.get("SUTUI_SERVER_TOKEN", "").strip()
    if single:
        return [single]
    from_file = _load_sutui_token()
    if from_file:
        return [from_file]
    return []


_sutui_tokens_list: List[str] = []
_sutui_token_index = 0
_sutui_token_lock = asyncio.Lock()


async def _next_sutui_token() -> Optional[str]:
    """从配置的多个速推 Token 中轮询取下一个（负载均衡）。"""
    global _sutui_tokens_list, _sutui_token_index
    if not _sutui_tokens_list:
        _sutui_tokens_list = _get_sutui_tokens_list()
    if not _sutui_tokens_list:
        return None
    async with _sutui_token_lock:
        idx = _sutui_token_index % len(_sutui_tokens_list)
        _sutui_token_index += 1
        return _sutui_tokens_list[idx]


def _parse_sse_or_json(resp: httpx.Response) -> Dict[str, Any]:
    """Parse response that may be JSON or SSE (text/event-stream)."""
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.text.strip()
    if "text/event-stream" in ct or raw.startswith("event:") or raw.startswith("data:"):
        last_data = ""
        for line in raw.splitlines():
            if line.startswith("data:"):
                last_data = line[5:].strip()
        if last_data:
            return json.loads(last_data)
        return {"error": {"message": f"Empty SSE stream from upstream (status={resp.status_code})"}}
    return resp.json()


_SUTUI_UPSTREAM_LOG_MAX = 500_000


def _dict_looks_like_account_balance(d: dict) -> bool:
    """含余额语义时，避免把字段名 credits 误当作「本次消耗」。"""
    kl = {str(k).lower() for k in d}
    return bool(kl & {"balance", "remaining", "remaining_credits", "total_balance", "available", "points"})


def _extract_sutui_credits_used(obj: Any, _depth: int = 0) -> int:
    """从速推 MCP JSON-RPC 整棵响应里解析本次消耗积分（动态扣费）。取遍历到的最大正整数，避免嵌套重复偏小。"""
    if _depth > 42:
        return 0
    best = 0
    if isinstance(obj, dict):
        balance_shape = _dict_looks_like_account_balance(obj)
        for k, v in obj.items():
            lk = str(k).lower()
            # price：xskill 官方 REST 创建任务返回里表示本次消耗积分（见 xskill-ai/scripts/xskill_api.py run_task）
            if lk in (
                "credits_used",
                "credits_charged",
                "credit_cost",
                "consumed_credits",
                "usage_credits",
                "cost",
                "price",
            ):
                if isinstance(v, (int, float)) and v > 0:
                    best = max(best, int(v))
            elif lk == "credits" and isinstance(v, (int, float)) and v > 0 and not balance_shape:
                best = max(best, int(v))
            elif isinstance(v, (dict, list)):
                best = max(best, _extract_sutui_credits_used(v, _depth + 1))
            elif isinstance(v, str):
                s = v.strip()
                if s.startswith("{"):
                    try:
                        best = max(best, _extract_sutui_credits_used(json.loads(s), _depth + 1))
                    except Exception:
                        pass
    elif isinstance(obj, list):
        for it in obj:
            best = max(best, _extract_sutui_credits_used(it, _depth + 1))
    return best


# 速推：在 create/generate 时扣费（与 xskill REST tasks/create 的 price 一致）；get_result 仅轮询，不再扣费。
# 若任务终态失败，按此处记录的 task_id 向龙虾用户退款（与速推侧失败退款语义对齐）。
_MAX_TASK_BILLED_TRACK = 3000
_task_billed_on_create: "OrderedDict[str, int]" = OrderedDict()


def _remember_task_billed_credits(task_id: str, credits: int) -> None:
    if not task_id or credits <= 0:
        return
    _task_billed_on_create[task_id] = credits
    _task_billed_on_create.move_to_end(task_id)
    while len(_task_billed_on_create) > _MAX_TASK_BILLED_TRACK:
        _task_billed_on_create.popitem(last=False)


def _pop_task_billed_credits(task_id: str) -> int:
    if not task_id:
        return 0
    return int(_task_billed_on_create.pop(task_id, 0) or 0)


def _extract_task_id_from_sutui_response(obj: Any, _depth: int = 0) -> str:
    if _depth > 42 or obj is None:
        return ""
    if isinstance(obj, dict):
        for k in ("task_id", "taskId", "id"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            t = _extract_task_id_from_sutui_response(v, _depth + 1)
            if t:
                return t
    elif isinstance(obj, list):
        for it in obj:
            t = _extract_task_id_from_sutui_response(it, _depth + 1)
            if t:
                return t
    elif isinstance(obj, str):
        s = obj.strip()
        if s.startswith("{"):
            try:
                return _extract_task_id_from_sutui_response(json.loads(s), _depth + 1)
            except Exception:
                pass
    return ""


def _sutui_get_result_is_terminal_failure(resp: Any) -> bool:
    """get_result 轮询终态且任务失败（非进行中、非成功完成）。"""
    if not isinstance(resp, dict):
        return False
    if resp.get("error"):
        return True
    if _is_task_still_in_progress(resp):
        return False
    raw = json.dumps(resp, ensure_ascii=False).lower()
    if '"status":"failed"' in raw or '"status": "failed"' in raw:
        return True
    if '"status":"error"' in raw or '"status": "error"' in raw:
        return True
    if '"status":"cancelled"' in raw or '"status":"canceled"' in raw:
        return True
    if "任务失败" in raw or "生成失败" in raw:
        return True
    return False


def _sutui_get_result_is_terminal_success(resp: Any) -> bool:
    if not isinstance(resp, dict) or resp.get("error"):
        return False
    if _is_task_still_in_progress(resp):
        return False
    raw = json.dumps(resp, ensure_ascii=False).lower()
    if '"status":"completed"' in raw or '"status": "completed"' in raw:
        return True
    if '"status":"success"' in raw or '"status": "success"' in raw:
        return True
    if "已完成" in raw or "生成成功" in raw:
        return True
    return False


def _sutui_phase_label(tool_name: str) -> str:
    """说明速推 MCP 工具与扣费阶段关系（具体以返回 JSON 为准）。"""
    if tool_name == "generate":
        return "创建任务|submit(generate)：提交文生图/视频任务，速推可能在此步扣积分或仅返回 task_id"
    if tool_name == "get_result":
        return "查询结果|poll(get_result)：轮询任务状态，速推可能在此步扣积分或返回成品 URL"
    return f"upstream_tool={tool_name}"


def _log_sutui_upstream_full_response(
    upstream_name: str,
    tool_name: str,
    lobster_capability_id: str,
    out: Any,
) -> None:
    """打印速推上游完整 JSON，便于对照「创建 vs 查询」哪一步出现积分字段。"""
    if upstream_name != "sutui":
        return
    try:
        parsed = _extract_sutui_credits_used(out) if isinstance(out, dict) else 0
        raw = json.dumps(out, ensure_ascii=False, default=str)
        total_len = len(raw)
        if total_len > _SUTUI_UPSTREAM_LOG_MAX:
            raw = raw[:_SUTUI_UPSTREAM_LOG_MAX] + f"\n... [已截断，原始总长 {total_len} 字符，可在 mcp/http_server.py 调大 _SUTUI_UPSTREAM_LOG_MAX]"
        logger.info(
            "[速推完整响应] %s | tool=%s | lobster_capability=%s | 计费解析credits=%s\n%s",
            _sutui_phase_label(tool_name),
            tool_name,
            lobster_capability_id or "(无)",
            parsed,
            raw,
        )
    except Exception as ex:
        logger.warning("[速推完整响应] 序列化失败 tool=%s: %s", tool_name, ex)


async def _call_upstream_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: Dict[str, Any],
    upstream_name: str = "",
    sutui_token: Optional[str] = None,
    lobster_capability_id: str = "",
) -> Dict[str, Any]:
    auth_headers: Dict[str, str] = {
        "Accept": "application/json, text/event-stream",
    }
    if upstream_name == "sutui":
        token = (sutui_token or "").strip()
        if not token:
            token = await _next_sutui_token()
        if token:
            auth_headers["Authorization"] = f"Bearer {token}"
        else:
            return {"error": {"message": "xskill/速推 Token 未配置。请在服务器配置 SUTUI_SERVER_TOKEN 或 SUTUI_SERVER_TOKENS（逗号分隔多个，负载均衡）。"}}

    async with httpx.AsyncClient(timeout=120.0) as client:
        init_body = {
            "jsonrpc": "2.0", "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "lobster-mcp-proxy", "version": "0.1.0"},
            },
        }
        try:
            init_resp = await client.post(server_url, json=init_body, headers=auth_headers)
        except httpx.HTTPError as exc:
            return {"error": {"message": f"无法连接上游 MCP ({server_url}): {exc}"}}

        if init_resp.status_code == 403:
            return {"error": {"message": "上游 MCP 认证失败 (403)。请检查 Token 是否正确。"}}
        if init_resp.status_code >= 400:
            return {"error": {"message": f"上游 MCP 初始化失败: HTTP {init_resp.status_code}"}}

        session_id = init_resp.headers.get("Mcp-Session-Id") or init_resp.headers.get("mcp-session-id") or ""
        if not session_id:
            try:
                ij = _parse_sse_or_json(init_resp)
                if isinstance(ij, dict):
                    r = ij.get("result") or {}
                    if isinstance(r, dict):
                        session_id = str(r.get("sessionId") or r.get("session_id") or "").strip()
            except Exception:
                pass
        call_body = {
            "jsonrpc": "2.0", "id": "call",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        call_headers = dict(auth_headers)
        if session_id:
            call_headers["Mcp-Session-Id"] = session_id
        try:
            r = await client.post(server_url, json=call_body, headers=call_headers)
        except httpx.HTTPError as exc:
            logger.warning("[MCP] 上游调用失败 tool=%s url=%s: %s", tool_name, server_url, exc)
            return {"error": {"message": f"上游工具调用失败: {exc}"}}

        if r.status_code >= 400:
            err_body = (r.text or "")[:_SUTUI_UPSTREAM_LOG_MAX]
            if upstream_name == "sutui":
                logger.warning(
                    "[速推完整响应] HTTP错误 phase=%s tool=%s lobster_capability=%s status=%s body=\n%s",
                    _sutui_phase_label(tool_name),
                    tool_name,
                    lobster_capability_id or "(无)",
                    r.status_code,
                    err_body,
                )
            else:
                logger.warning("[MCP] 上游返回 HTTP %s tool=%s: %s", r.status_code, tool_name, r.text[:200])
            return {"error": {"message": f"上游工具调用返回 HTTP {r.status_code}: {r.text[:300]}"}}
        try:
            out = _parse_sse_or_json(r)
            logger.info("[MCP] 上游调用完成 tool=%s status=%s", tool_name, r.status_code)
            _log_sutui_upstream_full_response(upstream_name, tool_name, lobster_capability_id, out)
            return out
        except Exception as e:
            logger.warning("[MCP] 上游响应解析失败 tool=%s: %s", tool_name, e)
            if upstream_name == "sutui":
                logger.warning(
                    "[速推完整响应] 解析失败 tool=%s lobster_capability=%s status=%s body=\n%s",
                    tool_name,
                    lobster_capability_id or "(无)",
                    r.status_code,
                    (r.text or "")[:_SUTUI_UPSTREAM_LOG_MAX],
                )
            return {"error": {"message": f"上游返回无法解析的响应: status={r.status_code}, body={r.text[:200]}"}}


# 速推 task 状态：先判进行中再判终态（与 backend 一致，避免「未完成」误判）
_TASK_TERMINAL = (
    "success", "completed", "done", "succeeded", "finished",
    "failed", "error", "cancelled", "canceled", "timeout", "expired",
    "已完成", "生成成功", "成功", "完成", "失败", "错误", "取消", "超时",
)
_TASK_IN_PROGRESS = (
    "pending", "queued", "submitted", "processing", "generating", "running",
    "处理中", "生成中", "排队中", "运行中", "上传中", "等待中",
)


def _is_task_still_in_progress(upstream_resp: Any) -> bool:
    """True if upstream get_result 表示任务仍在进行中。先判进行中再判终态（与 backend 一致）。"""
    if not isinstance(upstream_resp, dict):
        return False
    if upstream_resp.get("error"):
        return False
    raw = json.dumps(upstream_resp, ensure_ascii=False)
    raw_lower = raw.lower()
    for s in _TASK_IN_PROGRESS:
        if s in raw_lower or s in raw or f'"status":"{s}"' in raw_lower:
            return True
    for s in _TASK_TERMINAL:
        if s in raw_lower or s in raw or f'"status":"{s}"' in raw_lower:
            return False
    for content in (upstream_resp.get("content") or (upstream_resp.get("result") or {}).get("content") or []):
        if isinstance(content, dict) and (content.get("type") == "text" or "text" in content):
            t = (content.get("text") or "").lower()
            for s in _TASK_IN_PROGRESS:
                if s in t:
                    return True
            for s in _TASK_TERMINAL:
                if s in t:
                    return False
    return False


async def _record_call(token: Optional[str], capability_id: str, success: bool, latency_ms: Optional[int],
                       request_payload: Dict, response_payload: Optional[Dict], error_message: Optional[str],
                       credits_charged: Optional[int] = None, pre_deduct_applied: bool = False) -> None:
    if not token:
        return
    body = {
        "capability_id": capability_id, "success": success, "latency_ms": latency_ms,
        "request_payload": request_payload, "response_payload": response_payload,
        "error_message": (error_message or "")[:1000] or None, "source": "mcp_invoke",
        "chat_context_id": capability_id,
        "pre_deduct_applied": pre_deduct_applied,
    }
    if credits_charged is not None:
        body["credits_charged"] = credits_charged
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.post(f"{BASE_URL}/capabilities/record-call", json=body, headers=_backend_headers(token))
    except Exception:
        pass


_MEDIA_URL_RE = re.compile(r'https?://[^\s"\'<>\)\]]+\.(?:jpg|jpeg|png|webp|gif|mp4|webm|mov)', re.IGNORECASE)


def _normalize_image_generate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    按图片模型把「统一 payload」转成该模型 API 需要的参数，并保证用户输入的 prompt 原样传入。
    """
    if not payload or not isinstance(payload, dict):
        return payload
    model = (payload.get("model") or "").strip()
    prompt = (payload.get("prompt") or "").strip()
    image_url = (payload.get("image_url") or "").strip()
    image_size = (payload.get("image_size") or "").strip()
    num_images = payload.get("num_images", payload.get("n", 1))
    if isinstance(num_images, (int, float)):
        num_images = max(1, int(num_images))

    # jimeng-4.0 / jimeng-4.5：prompt 必填，image_url 可选，n
    if "jimeng-" in model:
        out: Dict[str, Any] = {"model": model, "prompt": prompt, "n": num_images}
        if image_url:
            out["image_url"] = image_url
        return out

    # fal-ai/flux-2/flash：prompt, image_urls 数组（图生图）, image_size, num_images
    if "flux-2/flash" in model or "flux-2" in model:
        out = {"model": model, "prompt": prompt, "image_size": image_size or "landscape_4_3", "num_images": num_images}
        if image_url:
            out["image_urls"] = [image_url]
        return out

    # fal-ai/bytedance/seedream/*：prompt, image_size, num_images
    if "seedream" in model:
        return {"model": model, "prompt": prompt, "image_size": image_size or "auto_2K", "num_images": num_images}

    # fal-ai/nano-banana-pro、nano-banana-2：prompt, image_urls 数组（可选）, aspect_ratio, num_images
    if "nano-banana" in model:
        out = {"model": model, "prompt": prompt, "aspect_ratio": (payload.get("aspect_ratio") or "1:1").strip(), "num_images": num_images}
        if image_url:
            out["image_urls"] = [image_url]
        return out

    # 其他图片模型：原样传，但保证 prompt 存在
    out = dict(payload)
    if "model" not in out:
        out["model"] = model
    out["prompt"] = prompt
    return out


def _normalize_video_generate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    按视频模型把「统一 payload」转成该模型 API 需要的参数，与 lobster 对齐：支持 backend 注入的 filePaths/media_files。
    """
    if not payload or not isinstance(payload, dict):
        return payload
    model = (payload.get("model") or "").strip()
    prompt = (payload.get("prompt") or "").strip()
    fp = payload.get("filePaths") or []
    image_url = (payload.get("image_url") or "").strip()
    mf = payload.get("media_files") or []
    
    # 模型名称到标准 ID 的映射（将界面展示名转换为速推/xskill 标准模型 ID）
    # 注意：这里只处理常见的展示名，标准 ID 格式（如 fal-ai/xxx）直接透传
    model_lower = model.lower()
    has_image = bool(fp) or bool(image_url) or bool(mf)
    
    if "/" not in model or not model.startswith(("fal-ai/", "st-ai/", "wan/", "jimeng-", "openai/", "anthropic/", "google/", "xai/")):
        # 可能是展示名，尝试映射到标准模型 ID
        if "sora" in model_lower and ("2" in model or "pub" in model_lower or "vip" in model_lower or "pro" in model_lower):
            # Sora 2 系列：根据是否有图片决定是 i2v 还是 t2v
            if "pub" in model_lower:
                model = "fal-ai/sora-2/image-to-video" if has_image else "fal-ai/sora-2/text-to-video"
            elif "vip" in model_lower:
                model = "fal-ai/sora-2/vip/image-to-video" if has_image else "fal-ai/sora-2/vip/text-to-video"
            elif "pro" in model_lower:
                model = "fal-ai/sora-2/pro/image-to-video" if has_image else "fal-ai/sora-2/pro/text-to-video"
            else:
                # 默认 Sora 2
                model = "fal-ai/sora-2/image-to-video" if has_image else "fal-ai/sora-2/text-to-video"
        elif "seedance" in model_lower or ("seed" in model_lower and "seedream" not in model_lower):
            if "2" in model or "2.0" in model:
                model = "st-ai/super-seed2"
            elif "1.5" in model:
                # Seedance 1.5 需要根据 task_type 判断
                if "text" in model_lower or "t2v" in model_lower:
                    model = "fal-ai/bytedance/seedance/v1.5/pro/text-to-video"
                else:
                    model = "fal-ai/bytedance/seedance/v1.5/pro/image-to-video" if has_image else "fal-ai/bytedance/seedance/v1.5/pro/text-to-video"
            elif "1" in model and "1.5" not in model:
                if "fast" in model_lower:
                    model = "fal-ai/bytedance/seedance/v1/pro/fast/image-to-video" if has_image else "fal-ai/bytedance/seedance/v1/pro/fast/text-to-video"
                elif "lite" in model_lower:
                    if "reference" in model_lower or "ref" in model_lower:
                        model = "fal-ai/bytedance/seedance/v1/lite/reference-to-video"
                    else:
                        model = "fal-ai/bytedance/seedance/v1/lite/image-to-video" if has_image else "fal-ai/bytedance/seedance/v1/lite/text-to-video"
                else:
                    model = "fal-ai/bytedance/seedance/v1/pro/image-to-video" if has_image else "fal-ai/bytedance/seedance/v1/pro/text-to-video"
        elif "kling" in model_lower:
            if "o3" in model_lower and "pro" in model_lower:
                model = "fal-ai/kling-video/o3/pro/image-to-video" if has_image else "fal-ai/kling-video/o3/pro/text-to-video"
            elif "o3" in model_lower:
                model = "fal-ai/kling-video/o3/image-to-video" if has_image else "fal-ai/kling-video/o3/text-to-video"
            else:
                model = "fal-ai/kling-video/image-to-video" if has_image else "fal-ai/kling-video/text-to-video"
        elif "wan" in model_lower or "万" in model:
            if "2.6" in model or "v2.6" in model_lower:
                model = "wan/v2.6/image-to-video" if has_image else "wan/v2.6/text-to-video"
            else:
                model = "wan/v2.6/image-to-video" if has_image else "wan/v2.6/text-to-video"
        elif "veo" in model_lower:
            if "3.1" in model:
                model = "google/veo-3.1/image-to-video" if has_image else "google/veo-3.1/text-to-video"
            else:
                model = "google/veo-3.1/image-to-video" if has_image else "google/veo-3.1/text-to-video"
        elif "grok" in model_lower:
            model = "xai/grok-imagine-video/image-to-video" if has_image else "xai/grok-imagine-video/text-to-video"
        # 注意：即梦主要是图片模型，视频模型较少，这里先不处理
    
    # 更新 model_lower 以反映映射后的模型 ID
    model_lower = model.lower()
    first_url = (str(fp[0]) if fp else "") or image_url or (str(mf[0]) if mf else "")
    if not first_url and image_url:
        first_url = image_url
    aspect_ratio = (payload.get("aspect_ratio") or "16:9").strip()
    duration = payload.get("duration")
    if duration is None:
        duration = 5
    valid_ratios = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
    ratio_ok = aspect_ratio in valid_ratios

    # st-ai/super-seed2：ratio, filePaths, functionMode（保留 backend 注入的多图 filePaths）
    if "super-seed2" in model or "st-ai/super-seed2" == model:
        out: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "functionMode": "first_last_frames",
            "ratio": aspect_ratio if ratio_ok else "16:9",
            "duration": int(duration) if duration is not None else 5,
        }
        out["filePaths"] = list(fp) if fp else ([first_url] if first_url else [])
        return out

    # wan/v2.6/*：duration 为字符串，i2v 用 image_url，t2v 用 aspect_ratio
    if "wan/v2.6" in model or "wan/" in model:
        out = {"model": model, "prompt": prompt, "duration": str(int(duration) if duration is not None else 5)}
        if "image-to-video" in model and first_url:
            out["image_url"] = first_url
        if "text-to-video" in model or not first_url:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        if payload.get("resolution"):
            out["resolution"] = str(payload.get("resolution", "1080p"))
        return out

    # fal-ai/minimax/hailuo*：prompt, image_url（i2v）
    if "hailuo" in model or "minimax" in model:
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        return out

    # fal-ai/vidu/q3/*：i2v 必填 image_url，t2v 无 image_url；duration(int)
    if "vidu" in model:
        out = {"model": model, "prompt": prompt or "", "duration": int(duration) if duration is not None else 5}
        if "image-to-video" in model and first_url:
            out["image_url"] = first_url
        if payload.get("resolution"):
            out["resolution"] = str(payload.get("resolution", "720p"))
        return out

    # fal-ai/bytedance/seedance/v1/* 和 v1.5/*：i2v 必填 image_url，duration 字符串, aspect_ratio
    # 注意：v1 和 v1.5 使用 options 对象包裹额外参数（resolution, generate_audio, camera_fixed, seed, end_image_url 等）
    if "seedance/v1" in model or "/seedance/v1/" in model or "seedance/v1.5" in model or "/seedance/v1.5/" in model:
        out = {
            "model": model,
            "prompt": prompt,
            "duration": str(int(duration) if duration is not None else 5),
        }
        # aspect_ratio 在顶层（v1.5 和 v1 都支持）
        if aspect_ratio and ratio_ok:
            out["aspect_ratio"] = aspect_ratio
        # image_url 在顶层（i2v 时）
        if "image-to-video" in model and first_url:
            out["image_url"] = first_url
        # 额外参数放入 options 对象（根据 xskill 文档）
        options: Dict[str, Any] = {}
        if payload.get("resolution"):
            options["resolution"] = str(payload.get("resolution", "720p"))
        if payload.get("generate_audio") is not None:
            options["generate_audio"] = bool(payload.get("generate_audio"))
        if payload.get("camera_fixed") is not None:
            options["camera_fixed"] = bool(payload.get("camera_fixed"))
        if payload.get("seed") is not None:
            options["seed"] = int(payload.get("seed"))
        if payload.get("end_image_url"):
            options["end_image_url"] = str(payload.get("end_image_url"))
        if payload.get("reference_image_urls"):
            options["reference_image_urls"] = payload.get("reference_image_urls")
        if payload.get("enable_safety_checker") is not None:
            options["enable_safety_checker"] = bool(payload.get("enable_safety_checker"))
        # 合并用户传入的 options（如果有）
        if payload.get("options") and isinstance(payload.get("options"), dict):
            options.update(payload.get("options"))
        if options:
            out["options"] = options
        return out

    # Sora 2 系列（sora-2/pub, sora-2/vip, sora-2/pro）：通用格式，i2v 用 image_url，t2v 用 aspect_ratio
    if "sora-2" in model.lower() or "sora" in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        if not first_url and aspect_ratio:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        if duration is not None:
            out["duration"] = int(duration)
        # 保留用户传入的其他参数（如 resolution 等）
        for k in ["resolution", "audio", "seed", "negative_prompt"]:
            if k in payload:
                out[k] = payload[k]
        return out

    # Kling 系列（kling-video, kling-o3）：i2v 用 image_url，支持 duration 和 resolution
    if "kling" in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        # 文生视频时，如果没有 aspect_ratio，添加默认值
        if not first_url and "aspect_ratio" not in payload and aspect_ratio:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        if duration is not None:
            out["duration"] = int(duration)
        if payload.get("resolution"):
            out["resolution"] = str(payload.get("resolution", "1080p"))
        # 保留其他参数
        for k in ["audio", "seed", "negative_prompt", "aspect_ratio"]:
            if k in payload:
                out[k] = payload[k]
        return out

    # Veo 3.1 系列：i2v 用 image_url，支持 duration 和 resolution
    if "veo" in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        # 文生视频时，如果没有 aspect_ratio，添加默认值
        if not first_url and "aspect_ratio" not in payload and aspect_ratio:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        if duration is not None:
            out["duration"] = int(duration)
        if payload.get("resolution"):
            out["resolution"] = str(payload.get("resolution", "1080p"))
        # 保留其他参数
        for k in ["audio", "seed", "negative_prompt", "aspect_ratio"]:
            if k in payload:
                out[k] = payload[k]
        return out

    # Grok Imagine Video：i2v 用 image_url，支持 duration
    if "grok" in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        # 文生视频时，如果没有 aspect_ratio，添加默认值
        if not first_url and "aspect_ratio" not in payload and aspect_ratio:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        if duration is not None:
            out["duration"] = int(duration)
        # 保留其他参数
        for k in ["audio", "seed", "negative_prompt", "aspect_ratio", "resolution"]:
            if k in payload:
                out[k] = payload[k]
        return out

    # 即梦系列（jimeng）：i2v 用 image_url，支持 duration
    if "jimeng" in model.lower() or "即梦" in model:
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        # 文生视频时，如果没有 aspect_ratio，添加默认值
        if not first_url and "aspect_ratio" not in payload and aspect_ratio:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        if duration is not None:
            out["duration"] = int(duration)
        # 保留用户传入的 aspect_ratio 或其他参数
        if aspect_ratio and ratio_ok and "aspect_ratio" not in out:
            out["aspect_ratio"] = aspect_ratio
        for k in ["resolution", "audio", "seed", "negative_prompt", "aspect_ratio"]:
            if k in payload:
                out[k] = payload[k]
        return out

    # 其他视频模型：通用处理，确保基本参数正确传递
    # 1. 图生视频（有 image_url/filePaths/media_files）：传递 image_url
    # 2. 文生视频（无图片）：传递 aspect_ratio
    # 3. 保留所有用户传入的参数，不做过滤
    out = dict(payload)
    if "model" not in out:
        out["model"] = model
    if "prompt" not in out or not out.get("prompt"):
        out["prompt"] = prompt
    
    # 统一处理图片 URL：优先使用 backend 注入的 filePaths/media_files
    if first_url and "image_url" not in out:
        out["image_url"] = first_url
    elif first_url:
        # 如果已有 image_url 但 backend 注入了新的，优先用新的
        out["image_url"] = first_url
    
    # 文生视频时，如果没有 aspect_ratio，添加默认值
    if not first_url and "aspect_ratio" not in out and aspect_ratio:
        out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
    
    # 如果没有 duration，添加默认值
    if "duration" not in out and duration is not None:
        out["duration"] = duration
    
    return out


async def _auto_save_generated_assets(
    upstream_resp: Any, capability_id: str, payload: Dict, token: Optional[str],
) -> List[Dict[str, str]]:
    """Extract media URLs from upstream result and auto-save as local assets."""
    if not token:
        return []
    raw = json.dumps(upstream_resp, ensure_ascii=False) if isinstance(upstream_resp, dict) else str(upstream_resp)
    urls = list(dict.fromkeys(_MEDIA_URL_RE.findall(raw)))
    if not urls:
        return []

    prompt_text = payload.get("prompt", "") or capability_id
    media_type = "video" if capability_id.startswith("video") else "image"
    saved: List[Dict[str, str]] = []
    for url in urls[:5]:
        if url.lower().endswith((".mp4", ".webm", ".mov")):
            mt = "video"
        else:
            mt = "image"
        body = {
            "url": url,
            "media_type": mt or media_type,
            "prompt": prompt_text[:500],
            "tags": f"auto,{capability_id}",
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(f"{BASE_URL}/api/assets/save-url", json=body, headers=_backend_headers(token))
            if r.status_code < 400:
                d = r.json()
                saved.append({"asset_id": d.get("asset_id", ""), "filename": d.get("filename", ""), "media_type": mt or media_type})
        except Exception:
            pass
    return saved


async def _call_tool(name: str, args: Dict[str, Any], token: Optional[str], request: Optional[Request] = None) -> Tuple[List[Dict[str, Any]], bool]:
    try:
        catalog = _load_capability_catalog()
        upstream_urls = _load_upstream_urls()

        if name == "list_capabilities":
            data = {"capabilities": [{"capability_id": cid, "description": catalog[cid].get("description") or cid} for cid in sorted(catalog.keys()) if catalog[cid].get("enabled") is not False]}
            return [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}], False

        if name == "manage_skills":
            action = (args.get("action") or "").strip()
            package_id = (args.get("package_id") or "").strip()
            query = (args.get("query") or "").strip()
            mcp_name = (args.get("name") or "").strip()
            mcp_url = (args.get("url") or "").strip()

            if action == "search_online":
                if not query:
                    return [{"type": "text", "text": "请提供 query 参数，如 'image', 'database', 'github'"}], True
                async with httpx.AsyncClient(timeout=60.0) as client:
                    # Browse a few pages first to populate cache
                    for pg in range(1, 4):
                        await client.get(
                            f"{BASE_URL}/api/mcp-registry/browse",
                            params={"page": str(pg)},
                            headers=_backend_headers(token),
                        )
                    # Now search the cache
                    r = await client.get(
                        f"{BASE_URL}/api/mcp-registry/search",
                        params={"q": query, "page_size": "20"},
                        headers=_backend_headers(token),
                    )
                data = r.json() if r.content else {}
                servers = data.get("servers", [])
                if not servers:
                    return [{"type": "text", "text": f"未找到与 '{query}' 相关的技能。试试其他关键词：image, video, database, search, github, slack, filesystem"}], False
                lines = [f"找到 {len(servers)} 个与 '{query}' 相关的 MCP 技能：\n"]
                for i, srv in enumerate(servers, 1):
                    lines.append(f"{i}. **{srv.get('title', srv.get('name', ''))}**")
                    if srv.get("description"):
                        lines.append(f"   {srv['description'][:120]}")
                    if srv.get("remote_url"):
                        lines.append(f"   URL: {srv['remote_url']}")
                        lines.append(f"   → 可通过 add_mcp 添加: name=\"{srv.get('name', '').replace('/', '_')}\", url=\"{srv['remote_url']}\"")
                    elif srv.get("install_cmd"):
                        lines.append(f"   安装命令: {srv['install_cmd']}")
                    lines.append("")
                return [{"type": "text", "text": "\n".join(lines)}], False

            if action == "add_mcp":
                if not mcp_name or not mcp_url:
                    return [{"type": "text", "text": "add_mcp 需要 name 和 url 参数"}], True
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.post(
                        f"{BASE_URL}/skills/add-mcp",
                        json={"name": mcp_name, "url": mcp_url},
                        headers=_backend_headers(token),
                    )
                return [{"type": "text", "text": json.dumps(r.json() if r.content else {}, ensure_ascii=False, indent=2)}], r.status_code >= 400

            async with httpx.AsyncClient(timeout=30.0) as client:
                if action == "list_store":
                    r = await client.get(f"{BASE_URL}/skills/store", headers=_backend_headers(token))
                elif action == "list_installed":
                    r = await client.get(f"{BASE_URL}/skills/installed", headers=_backend_headers(token))
                elif action == "install":
                    if not package_id:
                        return [{"type": "text", "text": "请提供 package_id"}], True
                    r = await client.post(f"{BASE_URL}/skills/install", json={"package_id": package_id}, headers=_backend_headers(token))
                elif action == "uninstall":
                    if not package_id:
                        return [{"type": "text", "text": "请提供 package_id"}], True
                    r = await client.post(f"{BASE_URL}/skills/uninstall", json={"package_id": package_id}, headers=_backend_headers(token))
                else:
                    return [{"type": "text", "text": f"未知操作: {action}。支持: list_store, list_installed, install, uninstall, search_online, add_mcp"}], True
            return [{"type": "text", "text": json.dumps(r.json() if r.content else {}, ensure_ascii=False, indent=2)}], r.status_code >= 400

        if name == "invoke_capability":
            capability_id = (args.get("capability_id") or "").strip()
            payload = args.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            if not capability_id or capability_id not in catalog:
                return [{"type": "text", "text": f"能力未找到: {capability_id}"}], True
            cfg = catalog[capability_id]
            upstream_tool = str(cfg.get("upstream_tool") or "").strip()
            if not upstream_tool:
                return [{"type": "text", "text": f"能力配置缺失 upstream_tool: {capability_id}"}], True
            upstream_name = str(cfg.get("upstream") or "sutui").strip()
            upstream_url = upstream_urls.get(upstream_name, "").strip()
            if not upstream_url:
                return [{"type": "text", "text": f"未配置上游网关: {upstream_name}，请在 .env 或技能商店中配置"}], True
            pre_deduct_amount = 0
            if token:
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        pre_r = await client.post(
                            f"{BASE_URL}/capabilities/pre-deduct",
                            json={"capability_id": capability_id},
                            headers=_backend_headers(token),
                        )
                    if pre_r.status_code == 402:
                        detail = (pre_r.json() or {}).get("detail", "积分不足")
                        return [{"type": "text", "text": f"积分不足，无法调用能力。{detail}"}], True
                    if pre_r.status_code == 200:
                        pre_deduct_amount = (pre_r.json() or {}).get("credits_charged") or 0
                except Exception:
                    pass
            sutui_token = (request.headers.get("X-Sutui-Token") or "").strip() or None if request else None
            # 规范化 payload：根据 capability_id 调用相应的规范化函数
            if capability_id == "image.generate":
                payload = _normalize_image_generate_payload(payload)
            elif capability_id == "video.generate":
                payload = _normalize_video_generate_payload(payload)
            t0 = time.perf_counter()
            logger.info("[MCP] invoke_capability capability_id=%s upstream=%s", capability_id, upstream_name)
            upstream_resp = await _call_upstream_mcp_tool(
                upstream_url,
                upstream_tool,
                payload,
                upstream_name=upstream_name,
                sutui_token=sutui_token,
                lobster_capability_id=capability_id,
            )
            # task.get_result: 不再在此处轮询，由 backend chat 每 15s 轮询并写回对话
            latency_ms = int((time.perf_counter() - t0) * 1000)
            upstream_error = ""
            if isinstance(upstream_resp, dict):
                err_obj = upstream_resp.get("error")
                if isinstance(err_obj, dict):
                    upstream_error = str(err_obj.get("message") or "")[:500]
            if upstream_error and pre_deduct_amount > 0 and token:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await client.post(
                            f"{BASE_URL}/capabilities/refund",
                            json={"capability_id": capability_id, "credits": pre_deduct_amount},
                            headers=_backend_headers(token),
                        )
                except Exception:
                    pass

            poll_task_id = (payload.get("task_id") or payload.get("taskId") or "").strip()
            # get_result 终态失败：创建任务时已扣的积分退回龙虾用户（速推侧失败退款时与本机余额对齐）
            if (
                token
                and upstream_tool == "get_result"
                and poll_task_id
                and isinstance(upstream_resp, dict)
                and not upstream_error
                and _sutui_get_result_is_terminal_failure(upstream_resp)
            ):
                refund_amt = _pop_task_billed_credits(poll_task_id)
                if refund_amt > 0:
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            await client.post(
                                f"{BASE_URL}/capabilities/refund",
                                json={"capability_id": capability_id, "credits": refund_amt},
                                headers=_backend_headers(token),
                            )
                        logger.info(
                            "[MCP] 任务终态失败退款 task_id=%s credits=%s（与速推创建任务扣费对应）",
                            poll_task_id,
                            refund_amt,
                        )
                    except Exception:
                        logger.exception("[MCP] 任务失败退款接口失败 task_id=%s", poll_task_id)
            elif (
                upstream_tool == "get_result"
                and poll_task_id
                and isinstance(upstream_resp, dict)
                and not upstream_error
                and _sutui_get_result_is_terminal_success(upstream_resp)
            ):
                dropped = _pop_task_billed_credits(poll_task_id)
                if dropped > 0:
                    logger.info("[MCP] 任务成功，清除创建扣费缓存 task_id=%s billed_was=%s", poll_task_id, dropped)

            actual_used = 0
            if isinstance(upstream_resp, dict) and not upstream_error:
                # 仅 generate（创建任务）按速推返回扣费；get_result 只轮询不重复扣
                if upstream_tool == "generate":
                    actual_used = _extract_sutui_credits_used(upstream_resp)
                elif upstream_tool == "get_result":
                    actual_used = 0
                else:
                    actual_used = _extract_sutui_credits_used(upstream_resp)
            if pre_deduct_amount > 0:
                bill_credits = pre_deduct_amount
                pre_applied_flag = True
            else:
                bill_credits = actual_used
                pre_applied_flag = False
            logger.info(
                "[MCP] invoke_capability 计费 capability_id=%s pre_deduct=%s upstream_parsed=%s bill=%s pre_applied=%s",
                capability_id, pre_deduct_amount, actual_used, bill_credits, pre_applied_flag,
            )
            await _record_call(
                token, capability_id, not bool(upstream_error), latency_ms, payload,
                upstream_resp if isinstance(upstream_resp, dict) else {}, upstream_error or None,
                credits_charged=(bill_credits if bill_credits > 0 else None),
                pre_deduct_applied=pre_applied_flag,
            )
            if (
                upstream_tool == "generate"
                and not upstream_error
                and bill_credits > 0
                and isinstance(upstream_resp, dict)
            ):
                created_tid = _extract_task_id_from_sutui_response(upstream_resp)
                if created_tid:
                    _remember_task_billed_credits(created_tid, bill_credits)
                    logger.info(
                        "[MCP] 已记录创建任务扣费 task_id=%s credits=%s（失败时凭 task_id 退款）",
                        created_tid,
                        bill_credits,
                    )
            logger.info("[MCP] invoke_capability 完成 capability_id=%s latency_ms=%s ok=%s", capability_id, latency_ms, not bool(upstream_error))
            data: Dict[str, Any] = {"capability_id": capability_id, "result": _redact_sensitive(upstream_resp)}
            if bill_credits > 0:
                data["credits_used"] = bill_credits

            if not upstream_error:
                saved = await _auto_save_generated_assets(upstream_resp, capability_id, payload, token)
                if saved:
                    data["saved_assets"] = saved

            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], bool(upstream_error)

        if name == "save_asset":
            url = (args.get("url") or "").strip()
            if not url:
                return [{"type": "text", "text": "请提供素材 URL"}], True
            body = {
                "url": url,
                "media_type": args.get("media_type", "image"),
                "tags": args.get("tags", ""),
                "prompt": args.get("prompt", ""),
            }
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(f"{BASE_URL}/api/assets/save-url", json=body, headers=_backend_headers(token))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "list_assets":
            params_qs: Dict[str, str] = {}
            if args.get("media_type"):
                params_qs["media_type"] = args["media_type"]
            if args.get("query"):
                params_qs["q"] = args["query"]
            params_qs["limit"] = str(args.get("limit", 20))
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{BASE_URL}/api/assets", params=params_qs, headers=_backend_headers(token))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "list_publish_accounts":
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{BASE_URL}/api/accounts", headers=_backend_headers(token))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "open_account_browser":
            nickname = (args.get("account_nickname") or "").strip()
            if not nickname:
                return [{"type": "text", "text": "请提供 account_nickname"}], True
            acct_id = await _find_account_id_by_nickname(nickname, token)
            if not acct_id:
                return [{"type": "text", "text": f"找不到昵称为「{nickname}」的账号，请先在「发布管理」中添加"}], True
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(f"{BASE_URL}/api/accounts/{acct_id}/open-browser", headers=_backend_headers(token))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "check_account_login":
            nickname = (args.get("account_nickname") or "").strip()
            if not nickname:
                return [{"type": "text", "text": "请提供 account_nickname"}], True
            acct_id = await _find_account_id_by_nickname(nickname, token)
            if not acct_id:
                return [{"type": "text", "text": f"找不到昵称为「{nickname}」的账号"}], True
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{BASE_URL}/api/accounts/{acct_id}/login-status", headers=_backend_headers(token))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "publish_content":
            asset_id = (args.get("asset_id") or "").strip()
            account_nickname = (args.get("account_nickname") or "").strip()
            if not asset_id:
                return [{"type": "text", "text": "请提供 asset_id（通过 save_asset 获得）"}], True
            if not account_nickname:
                return [{"type": "text", "text": "请提供 account_nickname（通过 list_publish_accounts 查看）"}], True
            logger.info("[MCP] publish_content 调用: asset_id=%s account_nickname=%s", asset_id, account_nickname)
            body = {
                "asset_id": asset_id,
                "account_nickname": account_nickname,
                "title": args.get("title", ""),
                "description": args.get("description", ""),
                "tags": args.get("tags", ""),
                "cover_asset_id": args.get("cover_asset_id"),
                "options": args.get("options") if isinstance(args.get("options"), dict) else {},
            }
            async with httpx.AsyncClient(timeout=180.0) as client:
                r = await client.post(f"{BASE_URL}/api/publish", json=body, headers=_backend_headers(token))
            data = r.json() if r.content else {}
            logger.info("[MCP] publish_content 后端响应: status=%s body_status=%s",
                        r.status_code, data.get("status") if isinstance(data, dict) else None)
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        return [{"type": "text", "text": f"Unknown tool: {name}"}], True
    except Exception as e:
        return [{"type": "text", "text": f"调用出错: {e}"}], True


def _make_error(id_value: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_value, "error": {"code": code, "message": message}}


async def _handle_single_message(msg: Dict[str, Any], request: Request) -> Optional[Dict[str, Any]]:
    if not isinstance(msg, dict):
        return _make_error(None, -32600, "Invalid message")
    method = msg.get("method")
    msg_id = msg.get("id")
    if msg_id is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "lobster-mcp", "version": "0.1.0"},
            "instructions": "龙虾 AI 助手能力网关：图片生成、视频解析、语音合成、技能管理。",
        }}
    if method == "tools/list":
        catalog = _load_capability_catalog()
        tools = _tool_definitions(catalog)
        logger.info("[MCP] tools/list -> %s tools", len(tools))
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        cap_id = (arguments.get("capability_id") or "").strip() if name == "invoke_capability" else ""
        token = _get_token_from_request(request)
        logger.info("[MCP] tools/call name=%s capability_id=%s", name, cap_id or "-")
        content, is_error = await _call_tool(name, arguments, token, request=request)
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content, "isError": is_error}}
    return _make_error(msg_id, -32601, f"Method not found: {method}")


async def mcp_endpoint(request: Request) -> Response:
    if request.method == "GET":
        return PlainTextResponse("SSE not implemented", status_code=405)
    if request.method != "POST":
        return PlainTextResponse("Method not allowed", status_code=405)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    responses: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            resp = await _handle_single_message(item, request)
            if resp is not None:
                responses.append(resp)
    elif isinstance(payload, dict):
        resp = await _handle_single_message(payload, request)
        if resp is not None:
            responses.append(resp)
    else:
        return JSONResponse({"error": "Invalid payload"}, status_code=400)
    if not responses:
        return Response(status_code=202)
    if len(responses) == 1:
        return JSONResponse(responses[0])
    return JSONResponse(responses)


app = Starlette(
    routes=[Route("/mcp", mcp_endpoint, methods=["GET", "POST"])],
    middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["*"])],
)
