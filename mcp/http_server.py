"""
HTTP MCP Server for 龙虾 (Lobster).
Simplified from ai_test_platform: no admin checks, dynamic catalog reload.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import uuid
from decimal import Decimal
from collections import OrderedDict
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

_MCP_DIR = str(Path(__file__).resolve().parent)
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)

import httpx

from mcp.video_model_resolve import resolve_video_model_id
from mcp.sutui_error_hints import (
    append_capability_model_hint,
    enhance_upstream_rest_error,
    hint_for_wrong_capability_model,
)
from mcp.jwt_brand import resolve_brand_mark_for_request
from mcp.sutui_tokens import (
    next_sutui_server_token,
    next_sutui_server_token_with_pool,
    sutui_token_ref_from_secret,
)

from backend.app.services.credits_amount import quantize_credits
from backend.app.services.sutui_api_audit import log_xskill_http
from backend.app.services.sutui_pricing import extract_upstream_reported_credits

logger = logging.getLogger(__name__)


def _sanitize_for_json(obj: Any) -> Any:
    """速推/API 响应或计费字段中可能含 Decimal，json.dumps 无法直接序列化。"""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_sanitize_for_json(x) for x in obj)
    return obj


def _json_dumps_mcp_payload(obj: Any) -> str:
    return json.dumps(_sanitize_for_json(obj), ensure_ascii=False, indent=2)


from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route


BASE_URL = os.environ.get("AI_TEST_PLATFORM_BASE_URL", "http://localhost:8000").rstrip("/")
CAPABILITY_SUTUI_MCP_URL = os.environ.get("CAPABILITY_SUTUI_MCP_URL", "").strip()
CAPABILITY_UPSTREAM_URLS_JSON = os.environ.get("CAPABILITY_UPSTREAM_URLS_JSON", "").strip()
# 在线版素材应在用户本机（lobster_online）；云端 ECS 上 MCP 若开启自动入库，会写 ECS 的素材库。设为 0/false/off 则关闭 invoke_capability 后的自动 save-url（显式 save_asset 工具不受影响）。
_MCP_AUTOSAVE_FLAG = os.environ.get("MCP_AUTOSAVE_ASSETS", "0").strip().lower()
MCP_AUTOSAVE_ASSETS_ENABLED = _MCP_AUTOSAVE_FLAG in ("1", "true", "yes", "on")


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


_SKILL_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "skill_registry.json"
_DEBUG_ONLY_MCP_TOOL_NAMES = frozenset()


def _load_skill_registry() -> Dict[str, Any]:
    try:
        if _SKILL_REGISTRY_PATH.exists():
            return json.loads(_SKILL_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[MCP] skill_registry 读取失败: %s", e)
    return {"packages": {}}


def _capability_id_is_debug_only_in_registry(cap_id: str) -> bool:
    """能力仅出现在 store_visibility=debug 的包中、且未出现在 online 或未标注包时，对非管理员隐藏。"""
    registry = _load_skill_registry()
    found_online = False
    found_debug = False
    for pkg in (registry.get("packages") or {}).values():
        if not isinstance(pkg, dict):
            continue
        caps = pkg.get("capabilities") or {}
        if cap_id not in caps:
            continue
        vis = (pkg.get("store_visibility") or "").strip().lower()
        if vis == "debug":
            found_debug = True
        else:
            found_online = True
    if found_online:
        return False
    return found_debug


async def _fetch_is_skill_store_admin(token: Optional[str]) -> bool:
    if not (token or "").strip():
        return False
    auth = (token or "").strip()
    if not auth.lower().startswith("bearer "):
        auth = f"Bearer {auth}"
    auth_base = (os.environ.get("AUTH_SERVER_BASE") or "").strip().rstrip("/")
    if not auth_base:
        auth_base = BASE_URL
    url = f"{auth_base.rstrip('/')}/skills/skill-store-admin"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, headers={"Authorization": auth})
        if r.status_code != 200:
            logger.warning("[MCP] skill-store-admin HTTP %s", r.status_code)
            return False
        data = r.json()
        return bool(data.get("is_skill_store_admin"))
    except Exception as e:
        logger.warning("[MCP] skill-store-admin 请求失败: %s", e)
        return False


async def _fetch_user_allowed_capability_ids(token: Optional[str]) -> Optional[set]:
    """获取用户可使用的 capability_id 集合。管理员返回 None（不限制），匿名返回空集。"""
    if not (token or "").strip():
        return set()
    auth = (token or "").strip()
    if not auth.lower().startswith("bearer "):
        auth = f"Bearer {auth}"
    auth_base = (os.environ.get("AUTH_SERVER_BASE") or "").strip().rstrip("/")
    if not auth_base:
        auth_base = BASE_URL
    url = f"{auth_base.rstrip('/')}/skills/user-allowed-capability-ids"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, headers={"Authorization": auth})
        if r.status_code != 200:
            logger.warning("[MCP] user-allowed-capability-ids HTTP %s", r.status_code)
            return None
        data = r.json()
        if data.get("is_admin"):
            return None
        return set(data.get("capability_ids") or [])
    except Exception as e:
        logger.warning("[MCP] user-allowed-capability-ids 请求失败: %s", e)
        return None


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


def _backend_headers(token: Optional[str], request: Optional[Request] = None) -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if request is not None:
        xi = (request.headers.get("X-Installation-Id") or request.headers.get("x-installation-id") or "").strip()
        if xi:
            h["X-Installation-Id"] = xi
    bk = (os.environ.get("LOBSTER_MCP_BILLING_INTERNAL_KEY") or "").strip()
    if bk:
        h["X-Lobster-Mcp-Billing"] = bk
    return h


def _capabilities_api_base() -> str:
    """预扣/退还等优先直连 AUTH_SERVER_BASE（与线上一致），避免本机 BASE_URL 代理异常。"""
    auth = (os.environ.get("AUTH_SERVER_BASE") or "").strip().rstrip("/")
    if auth:
        return auth
    return BASE_URL.rstrip("/")


async def _find_account_id_by_nickname(
    nickname: str, token: Optional[str], request: Optional[Request] = None,
) -> Optional[int]:
    """Lookup account id by nickname from backend."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{BASE_URL}/api/accounts", headers=_backend_headers(token, request))
        if r.status_code == 200:
            for a in r.json().get("accounts", []):
                if a.get("nickname", "").strip() == nickname:
                    return a.get("id")
    except Exception:
        pass
    return None


def _tool_definitions(
    catalog: Dict[str, Dict[str, Any]],
    *,
    is_skill_store_admin: bool = True,
    allowed_capability_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    _HIDDEN_FROM_AI = {"comfly.veo", "comfly.veo.daihuo_pipeline"}
    capability_list = sorted(
        cid
        for cid in catalog.keys()
        if not (_capability_id_is_debug_only_in_registry(cid) and not is_skill_store_admin)
        and (allowed_capability_ids is None or cid in allowed_capability_ids)
        and cid not in _HIDDEN_FROM_AI
    )
    tools = [
        {
            "name": "list_capabilities",
            "description": "列出可用能力",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "invoke_capability",
            "description": (
                "调用能力(图片生成/视频/语音等)。"
                "【默认模型】image.generate 用户未指定模型时 payload.model 必须填 \"fal-ai/flux-2/flash\"（不要自动选 jimeng）；用户明确指定 jimeng-4.0/jimeng-4.5 等时正常使用。"
                "video.generate 用户未指定模型时 payload.model 填 \"sora2\"，用户未指定时长时 duration 必须填 4（即 4 秒）。"
                "【重要】用户指定 veo3.1/veo3.1-fast 等模型生成视频时，使用 capability_id=\"video.generate\"，payload.model 填用户指定的模型名（如 veo3.1）。系统会自动路由到最优上游。"
                "【爆款TVC】仅当用户明确说「TVC」「带货视频」时才用 capability_id=\"comfly.veo.daihuo_pipeline\"，不要仅因模型名含 veo 就选 comfly.veo。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "capability_id": {"type": "string", "enum": capability_list},
                    "payload": {"type": "object"},
                },
                "required": ["capability_id", "payload"],
            },
        },
        {
            "name": "manage_skills",
            "description": "管理技能包(list_store/list_installed/install/uninstall/search_online/add_mcp)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list_store", "list_installed", "install", "uninstall", "search_online", "add_mcp"]},
                    "package_id": {"type": "string"},
                    "query": {"type": "string"},
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["action"],
            },
        },
        {
            "name": "save_asset",
            "description": "保存素材到本地,返回asset_id",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "media_type": {"type": "string", "enum": ["image", "video", "audio"]},
                    "tags": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "list_assets",
            "description": "列出/搜索本地素材",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "media_type": {"type": "string", "enum": ["image", "video", "audio"]},
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
        {
            "name": "list_publish_accounts",
            "description": "列出发布账号(抖音/B站等)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "open_account_browser",
            "description": "打开账号浏览器窗口",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_nickname": {"type": "string"},
                },
                "required": ["account_nickname"],
            },
        },
        {
            "name": "check_account_login",
            "description": "检查账号是否已登录",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_nickname": {"type": "string"},
                },
                "required": ["account_nickname"],
            },
        },
        {
            "name": "publish_content",
            "description": "发布内容到平台(抖音/B站/头条等)。有素材时传asset_id；纯文字文章可只传title+description不传asset_id。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string", "description": "素材ID(可选，纯文字不需要)"},
                    "account_nickname": {"type": "string", "description": "账号昵称"},
                    "title": {"type": "string", "description": "标题"},
                    "description": {"type": "string", "description": "正文内容"},
                    "tags": {"type": "string", "description": "逗号分隔"},
                    "cover_asset_id": {"type": "string"},
                    "options": {"type": "object", "description": "平台参数(content_type:article/micro等)"},
                },
                "required": ["account_nickname"],
            },
        },
        {
            "name": "list_meta_social_accounts",
            "description": "列出IG/FB已连接账号",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "publish_meta_social",
            "description": "发布到IG(photo/video/carousel/reel/story)或FB(photo/video/link)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "platform": {"type": "string", "enum": ["instagram", "facebook"]},
                    "content_type": {"type": "string", "enum": ["photo", "video", "carousel", "reel", "story", "link"]},
                    "asset_id": {"type": "string"},
                    "image_url": {"type": "string"},
                    "video_url": {"type": "string"},
                    "caption": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "link": {"type": "string"},
                    "title": {"type": "string"},
                    "carousel_items": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["account_id", "platform", "content_type"],
            },
        },
        {
            "name": "get_meta_social_data",
            "description": "读取IG/FB帖子+Insights数据，先sync再get",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                    "platform": {"type": "string", "enum": ["instagram", "facebook"]},
                },
            },
        },
        {
            "name": "sync_meta_social_data",
            "description": "从IG/FB API拉取最新数据到本地",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                },
            },
        },
        {
            "name": "get_social_report",
            "description": "跨平台数据报告：IG+FB聚合摘要",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    if not is_skill_store_admin and _DEBUG_ONLY_MCP_TOOL_NAMES:
        tools = [t for t in tools if (t.get("name") or "") not in _DEBUG_ONLY_MCP_TOOL_NAMES]
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


# 速推：create/generate 响应里若带 price/credits_used 会立即记费；异步任务常见「创建时 0、完成时 get_result 才带消耗」——终态成功时须按 get_result 解析扣费（与速推实际扣费对齐）。
# 若任务终态失败，按此处记录的 task_id 向龙虾用户退款（与速推侧失败退款语义对齐）。
_MAX_TASK_BILLED_TRACK = 3000
_task_billed_on_create: "OrderedDict[str, Decimal]" = OrderedDict()


def _remember_task_billed_credits(task_id: str, credits: Decimal) -> None:
    if not task_id or credits <= 0:
        return
    _task_billed_on_create[task_id] = credits
    _task_billed_on_create.move_to_end(task_id)
    while len(_task_billed_on_create) > _MAX_TASK_BILLED_TRACK:
        _task_billed_on_create.popitem(last=False)


def _pop_task_billed_credits(task_id: str) -> Decimal:
    if not task_id:
        return Decimal(0)
    return quantize_credits(_task_billed_on_create.pop(task_id, 0) or 0)


def _peek_task_billed_credits(task_id: str) -> Decimal:
    if not task_id:
        return Decimal(0)
    return quantize_credits(_task_billed_on_create.get(task_id) or 0)


async def _post_task_failure_refund_with_retries(
    *,
    token: str,
    capability_id: str,
    poll_task_id: str,
    refund_amt: Decimal,
    request: Optional[Request],
    sutui_pool: Optional[str] = None,
    sutui_token_ref: Optional[str] = None,
) -> bool:
    """调用 backend /capabilities/refund；成功（HTTP<400）后由调用方 _pop 缓存。失败重试 3 次，避免瞬时网络导致「钱未退回」。"""
    url = f"{_capabilities_api_base()}/capabilities/refund"
    body: Dict[str, Any] = {"capability_id": capability_id, "credits": float(quantize_credits(refund_amt))}
    if (sutui_pool or "").strip() and (sutui_token_ref or "").strip():
        body["sutui_pool"] = sutui_pool.strip()
        body["sutui_token_ref"] = sutui_token_ref.strip()
    headers = _backend_headers(token, request)
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                r = await client.post(url, json=body, headers=headers)
            if r.status_code < 400:
                return True
            logger.warning(
                "[MCP] 任务失败退款 HTTP %s task_id=%s attempt=%s/3 body_prefix=%s",
                r.status_code,
                poll_task_id,
                attempt + 1,
                (r.text or "")[:160],
            )
        except Exception as e:
            logger.warning(
                "[MCP] 任务失败退款请求异常 task_id=%s attempt=%s/3: %s",
                poll_task_id,
                attempt + 1,
                e,
            )
        if attempt + 1 < 3:
            await asyncio.sleep(0.5 * (2**attempt))
    return False


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
    raw = json.dumps(_sanitize_for_json(resp), ensure_ascii=False).lower()
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
    raw = json.dumps(_sanitize_for_json(resp), ensure_ascii=False).lower()
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
        parsed = extract_upstream_reported_credits(out) if isinstance(out, dict) else quantize_credits(0)
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


def _log_sutui_task_terminal_failure_for_ops(data: Any, *, tool_name: str, lobster_capability_id: str) -> None:
    """tasks/query/create 的 data 终态失败时 WARNING，grep「速推任务失败」即可对齐 task_id / model / output。"""
    if not isinstance(data, dict):
        return
    st = str(data.get("status") or "").lower()
    out = data.get("output")
    has_err_obj = isinstance(out, dict) and bool(
        out.get("error") or out.get("raw_error") or out.get("error_code") or out.get("detail")
    )
    if st not in ("failed", "error", "cancelled", "canceled") and not has_err_obj:
        return
    tid = data.get("task_id") or data.get("request_id") or "(无)"
    model = data.get("model") or "(无)"
    parts: List[str] = []
    if isinstance(out, dict):
        for k in ("error", "error_code", "error_type", "raw_error", "detail", "message", "body"):
            if k in out and out[k] is not None:
                parts.append(f"{k}={out[k]!r}")
    logger.warning(
        "[速推任务失败] tool=%s capability=%s task_id=%s model=%s status=%s channel=%s | %s",
        tool_name,
        lobster_capability_id or "(无)",
        tid,
        model,
        st or "(空)",
        data.get("channel") or "(无)",
        " ".join(parts) if parts else f"output={out!r}",
    )
    try:
        full = json.dumps(_sanitize_for_json(data), ensure_ascii=False, default=str)
        if len(full) > _SUTUI_UPSTREAM_LOG_MAX:
            full = full[:_SUTUI_UPSTREAM_LOG_MAX] + "\n... [截断，可调 _SUTUI_UPSTREAM_LOG_MAX]"
        logger.warning("[速推任务失败] 完整 data（与速推 tasks 返回一致）:\n%s", full)
    except Exception as ex:
        logger.warning("[速推任务失败] data 序列化失败: %s", ex)


async def _call_upstream_sutui_tasks_rest(
    api_base: str,
    tool_name: str,
    arguments: Dict[str, Any],
    token: str,
    lobster_capability_id: str = "",
) -> Dict[str, Any]:
    """经 xskill 官方 REST `/api/v3/tasks/create` 与 `/api/v3/tasks/query` 调用，避免 MCP HTTP 在部分模型上返回 Decimal 序列化错误（-32603）。"""
    if not isinstance(arguments, dict):
        arguments = {}
    arguments = _sanitize_for_json(arguments)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # 与 save-url 一致：禁用系统代理，避免 HTTPS_PROXY 干扰访问 api.xskill.ai / 自建网关
    _SUTUI_NET_RETRY = 3
    _SUTUI_NET_EXC = (
        httpx.ConnectError,
        httpx.TimeoutException,
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        httpx.WriteTimeout,
    )
    model_for_hint = ""
    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        if tool_name == "generate":
            model = (arguments.get("model") or arguments.get("model_id") or "").strip()
            if not model:
                return {"error": {"message": "generate 缺少 model（或 model_id）"}}
            model_for_hint = model
            params = {k: v for k, v in arguments.items() if k not in ("model", "model_id")}
            body = {"model": model, "params": params, "channel": None}
            url = f"{api_base}/api/v3/tasks/create"
            logger.info(
                "[速推REST请求] POST tasks/create capability=%s model=%s params_keys=%s",
                lobster_capability_id or "(无)",
                model,
                sorted(params.keys()),
            )
            try:
                psum = dict(_sanitize_for_json(params)) if isinstance(params, dict) else params
                if isinstance(psum, dict) and isinstance(psum.get("prompt"), str) and len(psum["prompt"]) > 240:
                    psum["prompt"] = psum["prompt"][:240] + "…"
                logger.info(
                    "[速推REST请求] params 摘要 %s",
                    json.dumps(psum, ensure_ascii=False, default=str),
                )
            except Exception as ex:
                logger.warning("[速推REST请求] params 摘要失败: %s", ex)
        elif tool_name == "get_result":
            task_id = str(arguments.get("task_id") or "").strip()
            if not task_id:
                return {"error": {"message": "get_result 缺少 task_id"}}
            body = {"task_id": task_id}
            url = f"{api_base}/api/v3/tasks/query"
            logger.info(
                "[速推REST请求] POST tasks/query capability=%s task_id=%s",
                lobster_capability_id or "(无)",
                task_id,
            )
        else:
            return {"error": {"message": f"REST 上游未实现工具: {tool_name}"}}

        r: Optional[httpx.Response] = None
        last_net: Optional[BaseException] = None
        for attempt in range(_SUTUI_NET_RETRY):
            try:
                r = await client.post(url, json=body, headers=headers)
                last_net = None
                break
            except _SUTUI_NET_EXC as e:
                last_net = e
                logger.warning(
                    "[速推REST] 网络异常 tool=%s attempt=%s/%s lobster_capability=%s err=%s",
                    tool_name,
                    attempt + 1,
                    _SUTUI_NET_RETRY,
                    lobster_capability_id or "(无)",
                    e,
                )
                if attempt + 1 < _SUTUI_NET_RETRY:
                    await asyncio.sleep(1.0 * (attempt + 1))
        if last_net is not None:
            return {"error": {"message": f"上游网络不可达: {last_net}"}}
        assert r is not None

        if r.status_code >= 400:
            err_body = (r.text or "")[:_SUTUI_UPSTREAM_LOG_MAX]
            log_xskill_http(
                phase=f"tasks_rest.{tool_name}",
                method="POST",
                url=url,
                http_status=r.status_code,
                capability_or_model=lobster_capability_id or model_for_hint or "-",
                billing_snapshot=None,
                error_message=err_body[:8000],
                bearer_token=token,
                upstream_response=err_body,
            )
            logger.warning(
                "[速推完整响应] %s | tool=%s | lobster_capability=%s | REST HTTP=%s\n%s",
                _sutui_phase_label(tool_name),
                tool_name,
                lobster_capability_id or "(无)",
                r.status_code,
                err_body,
            )
            em = enhance_upstream_rest_error(
                http_status=r.status_code,
                err_body=(r.text or ""),
                capability_id=lobster_capability_id or "",
                model=model_for_hint,
            )
            return {"error": {"message": em}}
        try:
            payload = r.json()
        except Exception as e:
            raw = (r.text or "")[:_SUTUI_UPSTREAM_LOG_MAX]
            logger.warning(
                "[速推完整响应] %s | tool=%s | lobster_capability=%s | REST 非JSON err=%s\n%s",
                _sutui_phase_label(tool_name),
                tool_name,
                lobster_capability_id or "(无)",
                e,
                raw,
            )
            log_xskill_http(
                phase=f"tasks_rest.{tool_name}",
                method="POST",
                url=url,
                http_status=r.status_code,
                capability_or_model=lobster_capability_id or model_for_hint or "-",
                billing_snapshot=None,
                error_message=f"非JSON: {e}",
                bearer_token=token,
                upstream_response=raw,
            )
            return {"error": {"message": f"上游 REST 非 JSON: {e}"}}
        if not isinstance(payload, dict):
            logger.warning(
                "[速推完整响应] %s | tool=%s | lobster_capability=%s | REST 顶层非对象 body_prefix=%s",
                _sutui_phase_label(tool_name),
                tool_name,
                lobster_capability_id or "(无)",
                (r.text or "")[:800],
            )
            log_xskill_http(
                phase=f"tasks_rest.{tool_name}",
                method="POST",
                url=url,
                http_status=r.status_code,
                capability_or_model=lobster_capability_id or model_for_hint or "-",
                billing_snapshot=None,
                error_message="上游 REST 返回非对象",
                bearer_token=token,
                upstream_response=(r.text or "")[:_SUTUI_UPSTREAM_LOG_MAX],
            )
            return {"error": {"message": "上游 REST 返回非对象"}}
        code = payload.get("code")
        if code is not None and int(code) != 200:
            msg = payload.get("message") or payload.get("msg") or str(payload)
            log_xskill_http(
                phase=f"tasks_rest.{tool_name}",
                method="POST",
                url=url,
                http_status=200,
                capability_or_model=lobster_capability_id or model_for_hint or "-",
                billing_snapshot={"code": code, "message": str(msg)[:1500]},
                error_message=str(msg)[:8000],
                bearer_token=token,
                upstream_response=_sanitize_for_json(payload),
            )
            _log_sutui_upstream_full_response(
                "sutui", tool_name, lobster_capability_id, _sanitize_for_json(payload)
            )
            em = append_capability_model_hint(
                f"上游业务错误: {msg}",
                lobster_capability_id or "",
                model_for_hint,
            )
            return {"error": {"message": em}}
        data = payload.get("data")
        if not isinstance(data, dict):
            log_xskill_http(
                phase=f"tasks_rest.{tool_name}",
                method="POST",
                url=url,
                http_status=200,
                capability_or_model=lobster_capability_id or "-",
                billing_snapshot=None,
                error_message=f"无 data 对象: {str(payload)[:800]}",
                bearer_token=token,
                upstream_response=_sanitize_for_json(payload),
            )
            _log_sutui_upstream_full_response(
                "sutui", tool_name, lobster_capability_id, _sanitize_for_json(payload)
            )
            return {"error": {"message": f"上游 REST 无 data 对象: {str(payload)[:500]}"}}
        _log_sutui_upstream_full_response(
            "sutui", tool_name, lobster_capability_id, _sanitize_for_json(payload)
        )
        _log_sutui_task_terminal_failure_for_ops(data, tool_name=tool_name, lobster_capability_id=lobster_capability_id)
        log_xskill_http(
            phase=f"tasks_rest.{tool_name}",
            method="POST",
            url=url,
            http_status=200,
            capability_or_model=lobster_capability_id or model_for_hint or "-",
            billing_snapshot={
                "price": data.get("price"),
                "task_id": data.get("task_id"),
                "status": data.get("status"),
                "model": data.get("model"),
            },
            error_message="",
            bearer_token=token,
            upstream_response=_sanitize_for_json(payload),
        )
        return _sanitize_for_json(data)


async def _call_upstream_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: Dict[str, Any],
    upstream_name: str = "",
    sutui_token: Optional[str] = None,
    lobster_capability_id: str = "",
    brand_mark: Optional[str] = None,
) -> Dict[str, Any]:
    auth_headers: Dict[str, str] = {
        "Accept": "application/json, text/event-stream",
    }
    if upstream_name == "sutui":
        token = (sutui_token or "").strip()
        if not token:
            token = await next_sutui_server_token(brand_mark=brand_mark)
        if not token:
            return {"error": {"message": "xskill/速推 Token 未配置或当前品牌池为空。请配置 SUTUI_SERVER_TOKENS_BIHUO / SUTUI_SERVER_TOKENS_YINGSHI（及站内探测可选 SUTUI_SERVER_TOKEN）。终端用户须为 bihuo/yingshi，无 USER 兜底。MCP 需与 Backend 共用 SECRET_KEY 以解析 JWT brand_mark。"}}
        auth_headers["Authorization"] = f"Bearer {token}"
        # 实测 xskill MCP HTTP 在 generate 返回体序列化时抛 Decimal 错误；create/query 走 REST 稳定
        if tool_name in ("generate", "get_result"):
            api_base = os.environ.get("SUTUI_API_BASE", "https://api.xskill.ai").rstrip("/")
            return await _call_upstream_sutui_tasks_rest(
                api_base, tool_name, arguments, token, lobster_capability_id
            )

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
    raw = json.dumps(_sanitize_for_json(upstream_resp), ensure_ascii=False)
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
                       credits_charged: Optional[float] = None, pre_deduct_applied: bool = False,
                       credits_pre_deducted: Optional[float] = None, credits_final: Optional[float] = None,
                       request: Optional[Request] = None,
                       sutui_pool: Optional[str] = None, sutui_token_ref: Optional[str] = None) -> None:
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
    if credits_pre_deducted is not None:
        body["credits_pre_deducted"] = credits_pre_deducted
    if credits_final is not None:
        body["credits_final"] = credits_final
    if (sutui_pool or "").strip() and (sutui_token_ref or "").strip():
        body["sutui_pool"] = sutui_pool.strip()
        body["sutui_token_ref"] = sutui_token_ref.strip()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.post(
                f"{_capabilities_api_base()}/capabilities/record-call",
                json=_sanitize_for_json(body),
                headers=_backend_headers(token, request),
            )
    except Exception:
        pass


_MEDIA_URL_RE = re.compile(r'https?://[^\s"\'<>\)\]]+\.(?:jpg|jpeg|png|webp|gif|mp4|webm|mov)', re.IGNORECASE)

_VIDEO_ASPECT_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")


def _payload_get_aspect_ratio(payload: Dict[str, Any]) -> Any:
    """速推 / 前端可能用 ratio 或 aspect_ratio。"""
    if payload.get("aspect_ratio") is not None:
        return payload.get("aspect_ratio")
    if payload.get("ratio") is not None:
        return payload.get("ratio")
    for k in ("size", "image_size"):
        if payload.get(k) is not None:
            return payload.get(k)
    return None


def _payload_get_duration_raw(payload: Dict[str, Any]) -> Any:
    """duration / duration_seconds / length 等别名。"""
    for key in ("duration", "duration_seconds", "length", "video_length"):
        if payload.get(key) is not None:
            return payload.get(key)
    return None


def _coerce_video_aspect_ratio_for_upstream(raw: Any) -> str:
    """
    将 UI 与速推常见写法规范为 xskill 接受的宽高比枚举。
    无法识别时回退 16:9，避免上游 422（与官方参数容错一致）。
    """
    if raw is None or raw == "":
        return "16:9"
    ar = str(raw).strip()
    low = ar.lower().replace(" ", "")
    if low in ("auto", "automatic", "default", "original", "adapt"):
        return "16:9"
    if low in ("landscape", "横屏", "horizontal", "wide"):
        return "16:9"
    if low in ("portrait", "竖屏", "vertical", "tall"):
        return "9:16"
    if low in ("square", "1x1"):
        return "1:1"
    if "x" in ar and ":" not in ar:
        parts = ar.lower().replace(" ", "").replace("×", "x").split("x", 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            w, h = int(parts[0]), int(parts[1])
            if w > 0 and h > 0:
                g = math.gcd(w, h)
                ar = f"{w // g}:{h // g}"
    ar = ar.replace("：", ":").strip()
    if ar in _VIDEO_ASPECT_RATIOS:
        return ar
    ar2 = ar.replace(" ", "")
    if ar2 in _VIDEO_ASPECT_RATIOS:
        return ar2
    return "16:9"


# fal-ai/nano-banana-2 / nano-banana-pro 公开 schema 中的 aspect_ratio 枚举（勿与视频归一化混用）。
_NANO_BANANA_ASPECT_RATIOS = frozenset(
    {
        "auto",
        "21:9",
        "16:9",
        "3:2",
        "4:3",
        "5:4",
        "1:1",
        "4:5",
        "3:4",
        "2:3",
        "9:16",
        "4:1",
        "1:4",
        "8:1",
        "1:8",
    }
)


def _coerce_aspect_ratio_nano_banana(raw: Any) -> str:
    """
    nano-banana 文生图使用 fal 枚举；「auto」必须保持为 auto，不能用 _coerce_video_aspect_ratio_for_upstream（会把 auto 变成 16:9）。
    """
    if raw is None or raw == "":
        return "1:1"
    ar = str(raw).strip()
    low = ar.lower().replace(" ", "")
    if low in ("auto", "automatic", "default", "original", "adapt"):
        return "auto"
    if low in ("landscape", "横屏", "horizontal", "wide"):
        return "16:9"
    if low in ("portrait", "竖屏", "vertical", "tall"):
        return "9:16"
    if low in ("square", "1x1"):
        return "1:1"
    if "x" in ar and ":" not in ar:
        parts = ar.lower().replace(" ", "").split("x", 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            ar = f"{parts[0]}:{parts[1]}"
    ar = ar.replace("：", ":").strip()
    if ar in _NANO_BANANA_ASPECT_RATIOS:
        return ar
    ar2 = ar.replace(" ", "")
    if ar2 in _NANO_BANANA_ASPECT_RATIOS:
        return ar2
    coerced = _coerce_video_aspect_ratio_for_upstream(raw)
    if coerced in _NANO_BANANA_ASPECT_RATIOS:
        return coerced
    return "1:1"


def _parse_video_duration_seconds(raw: Any, *, default: int = 5) -> int:
    """解析 5、6s、\"10\" 等为整数秒；无法解析时用 default，避免抛错。"""
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return default
    try:
        if isinstance(raw, (int, float)):
            return max(1, int(raw))
        s = str(raw).strip().lower()
        if s.endswith("s"):
            s = s[:-1].strip()
        if not s:
            return default
        v = float(s)
        return max(1, int(round(v)))
    except (ValueError, TypeError, OverflowError):
        return default


# fal-ai/sora-2/*（含 text-to-video / image-to-video）上游 duration 枚举，非列表值易 422
_SORA_FAL_DURATION_SECONDS = (4, 8, 12, 16, 20)


def _coerce_sora_fal_duration_seconds(sec: int) -> int:
    """将秒数收敛到 fal Sora 2 允许的 duration；距离相同时取较小值。"""
    try:
        s = max(1, int(sec))
    except (ValueError, TypeError, OverflowError):
        return _SORA_FAL_DURATION_SECONDS[0]
    if s in _SORA_FAL_DURATION_SECONDS:
        return s
    best = _SORA_FAL_DURATION_SECONDS[0]
    best_d = abs(s - best)
    for a in _SORA_FAL_DURATION_SECONDS[1:]:
        d = abs(s - a)
        if d < best_d or (d == best_d and a < best):
            best, best_d = a, d
    return best


def _sanitize_video_resolution_value(raw: Any) -> Optional[str]:
    """UI 常见 resolution=auto 等占位：返回 None 表示不要传该字段，避免上游枚举校验 422。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower().replace(" ", "")
    if low in ("auto", "automatic", "default", "original"):
        return None
    return s


def _sanitize_options_dict_resolution(options: Dict[str, Any]) -> None:
    """Seedance 等 options.resolution 合并后去掉 auto 占位。"""
    if not isinstance(options, dict) or "resolution" not in options:
        return
    sr = _sanitize_video_resolution_value(options.get("resolution"))
    if sr is None:
        options.pop("resolution", None)
    else:
        options["resolution"] = sr


def _merge_common_video_ui_fields(out: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """合并速推 / xskill UI 常见顶层字段（不覆盖已写入的 model/prompt/image_url 等核心键）。"""
    for k in (
        "enable_prompt_expansion",
        "multi_shots",
        "enable_safety_checker",
        "resolution",
        "audio",
        "seed",
        "negative_prompt",
        "camera_fixed",
        "style",
        "mode",
        "fps",
        "cfg_scale",
        "motion_bucket_id",
        "consistency_with_text",
    ):
        if k == "resolution":
            if k in out:
                continue
            if k in payload and payload[k] is not None:
                sr = _sanitize_video_resolution_value(payload[k])
                if sr is not None:
                    out[k] = sr
            continue
        if k in payload and payload[k] is not None and k not in out:
            out[k] = payload[k]


def _clamp_num_images_for_image_model(num: int, model: str) -> int:
    """按速推 docs 常见上限收敛张数，避免 num_images 过大导致 422。"""
    m = (model or "").lower()
    n = max(1, int(num))
    if "seedream" in m:
        return min(n, 6)
    if "nano-banana" in m or "flux-2" in m or "qwen-image-edit" in m or m.startswith("jimeng-"):
        return min(n, 4)
    return n


_IMAGE_MODEL_ALIASES: Dict[str, str] = {
    "flux-2/flash": "fal-ai/flux-2/flash",
    "flux2/flash": "fal-ai/flux-2/flash",
    "flux2-flash": "fal-ai/flux-2/flash",
    "flux-2-flash": "fal-ai/flux-2/flash",
    "flux2": "fal-ai/flux-2/flash",
    "flux-2": "fal-ai/flux-2/flash",
}

_DEFAULT_IMAGE_MODEL = "fal-ai/flux-2/flash"


def _normalize_image_generate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    按图片模型把「统一 payload」转成该模型 API 需要的参数，并保证用户输入的 prompt 原样传入。
    """
    if not payload or not isinstance(payload, dict):
        return payload
    payload = dict(payload)
    model = (payload.get("model") or payload.get("model_id") or "").strip()
    if not model:
        model = _DEFAULT_IMAGE_MODEL
    model = _IMAGE_MODEL_ALIASES.get(model, model)
    payload["model"] = model
    prompt = (payload.get("prompt") or "").strip()
    image_url = (payload.get("image_url") or "").strip()
    image_size = (payload.get("image_size") or "").strip()
    num_images = payload.get("num_images", payload.get("n", 1))
    if isinstance(num_images, (int, float)):
        num_images = max(1, int(num_images))
    num_images = _clamp_num_images_for_image_model(num_images, model)

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

    # ── i2i 编辑模型：wan/v2.7/edit、seedream/*/edit、qwen-image-edit ──
    _is_edit = "/edit" in model or "image-edit" in model
    if _is_edit or "wan/v2.7" in model:
        _imgs = payload.get("image_urls") or ([image_url] if image_url else [])
        if isinstance(_imgs, str):
            _imgs = [_imgs]
        out = {"model": model, "prompt": prompt}
        if _imgs:
            out["image_urls"] = _imgs
        if image_size:
            out["image_size"] = image_size
        elif "seedream" in model:
            out["image_size"] = "auto_2K"
        if "qwen-image-edit" not in model:
            out["num_images"] = num_images
        neg = (payload.get("negative_prompt") or "").strip()
        if neg:
            out["negative_prompt"] = neg
        return out

    # fal-ai/bytedance/seedream/* (文生图)：prompt, image_size, num_images
    if "seedream" in model:
        return {"model": model, "prompt": prompt, "image_size": image_size or "auto_2K", "num_images": num_images}

    # fal-ai/nano-banana-pro、nano-banana-2：prompt, image_urls 数组（可选）, aspect_ratio, num_images
    if "nano-banana" in model:
        _ar = (payload.get("aspect_ratio") or payload.get("ratio") or "1:1")
        _ar = str(_ar).strip() if _ar is not None else "1:1"
        out = {
            "model": model,
            "prompt": prompt,
            "aspect_ratio": _coerce_aspect_ratio_nano_banana(_ar) if _ar else "1:1",
            "num_images": num_images,
        }
        if image_url:
            out["image_urls"] = [image_url]
        return out

    # 其他图片模型：原样传，但保证 prompt 存在，保留所有参数
    out = dict(payload)
    if "model" not in out:
        out["model"] = model
    if not out.get("prompt"):
        out["prompt"] = prompt
    # 确保所有用户传入的参数都被保留（包括 image_size, aspect_ratio, num_images, n 等）
    return out


def _normalize_understand_payload(
    payload: Dict[str, Any],
    media_key: str = "image_urls",
    default_model: str = "openrouter/router/vision",
) -> Dict[str, Any]:
    """将 image.understand / video.understand 的统一 payload 转成速推 generate 所需格式。"""
    if not payload or not isinstance(payload, dict):
        payload = {}
    payload = dict(payload)
    prompt = (payload.get("prompt") or "").strip() or "请详细描述内容。"
    model = (payload.get("model") or "").strip() or default_model

    urls = payload.get(media_key)
    if not urls:
        singular = media_key.replace("_urls", "_url")
        single = (payload.get(singular) or payload.get("image_url") or payload.get("video_url") or "").strip()
        if single:
            urls = [single]
    if isinstance(urls, str):
        urls = [urls]
    out: Dict[str, Any] = {"model": model, "prompt": prompt}
    if urls:
        out[media_key] = urls
    for k in ("system_prompt", "max_tokens", "temperature", "reasoning"):
        if k in payload:
            out[k] = payload[k]
    return out


_DEFAULT_VIDEO_MODEL = "sora2"


def _normalize_video_generate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    按视频模型把「统一 payload」转成该模型 API 需要的参数，与 lobster 对齐：支持 backend 注入的 filePaths/media_files。
    """
    if not payload or not isinstance(payload, dict):
        return payload
    model = (payload.get("model") or payload.get("model_id") or "").strip()
    if not model:
        model = _DEFAULT_VIDEO_MODEL
    prompt = (payload.get("prompt") or "").strip()
    fp = payload.get("filePaths") or []
    image_url = (payload.get("image_url") or "").strip()
    mf = payload.get("media_files") or []
    has_image = bool(fp) or bool(image_url) or bool(mf)

    model = resolve_video_model_id(model, has_image)
    model_lower = model.lower()
    first_url = (str(fp[0]) if fp else "") or image_url or (str(mf[0]) if mf else "")
    if not first_url and image_url:
        first_url = image_url
    aspect_ratio = _coerce_video_aspect_ratio_for_upstream(_payload_get_aspect_ratio(payload))
    valid_ratios = _VIDEO_ASPECT_RATIOS
    ratio_ok = aspect_ratio in valid_ratios
    duration_sec = _parse_video_duration_seconds(_payload_get_duration_raw(payload), default=5)

    # st-ai/super-seed2：ratio, filePaths, functionMode（保留 backend 注入的多图 filePaths）
    if "super-seed2" in model or "st-ai/super-seed2" == model:
        out: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "functionMode": "first_last_frames",
            "ratio": aspect_ratio if ratio_ok else "16:9",
            "duration": duration_sec,
        }
        out["filePaths"] = list(fp) if fp else ([first_url] if first_url else [])
        _merge_common_video_ui_fields(out, payload)
        return out

    # wan（v2.6 / v2.7）：duration 为字符串，i2v 用 image_url，t2v 用 aspect_ratio
    if model.startswith("wan/"):
        out = {"model": model, "prompt": prompt, "duration": str(duration_sec)}
        if "image-to-video" in model and first_url:
            out["image_url"] = first_url
        if "text-to-video" in model or not first_url:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        _wr = _sanitize_video_resolution_value(payload.get("resolution"))
        if _wr is not None:
            out["resolution"] = _wr
        _merge_common_video_ui_fields(out, payload)
        return out

    # fal-ai/minimax/hailuo*：Pro 无 duration（固定196积分），Standard 有 duration（字符串）
    if "hailuo" in model or "minimax" in model:
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        if "standard" in model_lower:
            out["duration"] = str(duration_sec)
        if payload.get("prompt_optimizer") is not None:
            out["prompt_optimizer"] = bool(payload["prompt_optimizer"])
        _merge_common_video_ui_fields(out, payload)
        return out

    # fal-ai/vidu/q3/*：i2v 必填 image_url，t2v 无 image_url；duration(int)
    if "vidu" in model:
        out = {"model": model, "prompt": prompt or "", "duration": duration_sec}
        if "image-to-video" in model and first_url:
            out["image_url"] = first_url
        _vr = _sanitize_video_resolution_value(payload.get("resolution"))
        if _vr is not None:
            out["resolution"] = _vr
        _merge_common_video_ui_fields(out, payload)
        return out

    # fal-ai/bytedance/seedance/v1/* 和 v1.5/*：i2v 必填 image_url，duration 字符串, aspect_ratio
    # 注意：v1 和 v1.5 使用 options 对象包裹额外参数（resolution, generate_audio, camera_fixed, seed, end_image_url 等）
    if "seedance/v1" in model or "/seedance/v1/" in model or "seedance/v1.5" in model or "/seedance/v1.5/" in model:
        out = {
            "model": model,
            "prompt": prompt,
            "duration": str(duration_sec),
        }
        # aspect_ratio 在顶层（v1.5 和 v1 都支持）
        if aspect_ratio and ratio_ok:
            out["aspect_ratio"] = aspect_ratio
        # image_url 在顶层（i2v 时）
        if "image-to-video" in model and first_url:
            out["image_url"] = first_url
        # 额外参数放入 options 对象（根据 xskill 文档）
        options: Dict[str, Any] = {}
        _sd_res = _sanitize_video_resolution_value(payload.get("resolution"))
        if _sd_res is not None:
            options["resolution"] = _sd_res
        if payload.get("generate_audio") is not None:
            options["generate_audio"] = bool(payload.get("generate_audio"))
        if payload.get("camera_fixed") is not None:
            options["camera_fixed"] = bool(payload.get("camera_fixed"))
        if payload.get("seed") is not None:
            try:
                options["seed"] = int(payload.get("seed"))
            except (ValueError, TypeError):
                options["seed"] = payload.get("seed")
        if payload.get("end_image_url"):
            options["end_image_url"] = str(payload.get("end_image_url"))
        if payload.get("reference_image_urls"):
            options["reference_image_urls"] = payload.get("reference_image_urls")
        if payload.get("enable_safety_checker") is not None:
            options["enable_safety_checker"] = bool(payload.get("enable_safety_checker"))
        for _k in ("enable_prompt_expansion", "multi_shots"):
            if payload.get(_k) is not None:
                options[_k] = bool(payload.get(_k))
        # 如果用户直接传了 options 对象，合并进去
        if payload.get("options") and isinstance(payload.get("options"), dict):
            options.update(payload.get("options"))
        _sanitize_options_dict_resolution(options)
        # 只有 options 不为空时才添加
        if options:
            out["options"] = options
        _merge_common_video_ui_fields(out, payload)
        return out

    # Sora 2 系列（sora-2/pub, sora-2/vip, sora-2/pro）：通用格式，i2v 用 image_url，t2v 用 aspect_ratio
    # resolution 由 _merge_common_video_ui_fields 统一净化（去掉 auto）。
    # duration：fal 仅允许 4/8/12/16/20；未传时用 4（与 fal 文档示例一致），勿用全局 default=5
    if "sora-2" in model.lower() or "sora" in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        _sora_d_raw = _payload_get_duration_raw(payload)
        _sora_sec = _parse_video_duration_seconds(_sora_d_raw, default=4)
        _sora_d = _coerce_sora_fal_duration_seconds(_sora_sec)
        if _sora_d != _sora_sec:
            logger.info(
                "[MCP] Sora duration 已收敛为 fal 枚举: raw=%r parsed=%s -> %s",
                _sora_d_raw,
                _sora_sec,
                _sora_d,
            )
        out["duration"] = _sora_d
        for k in ["audio", "seed", "negative_prompt"]:
            if k in payload:
                out[k] = payload[k]
        _merge_common_video_ui_fields(out, payload)
        return out

    # Kling 系列：duration 为字符串，音频参数为 generate_audio（非 audio）
    if "kling" in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        _has_ar = _payload_get_aspect_ratio(payload) is not None
        if not first_url or _has_ar:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        out["duration"] = str(duration_sec)
        if payload.get("generate_audio") is not None:
            out["generate_audio"] = bool(payload["generate_audio"])
        elif payload.get("audio") is not None:
            out["generate_audio"] = bool(payload["audio"])
        _merge_common_video_ui_fields(out, payload)
        return out

    # Veo 3.1 系列：i2v 用 image_url，支持 duration 和 resolution
    # duration 必须是字符串格式：'4s', '6s' 或 '8s'
    if "veo" in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        _has_ar = _payload_get_aspect_ratio(payload) is not None
        if not first_url or _has_ar:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        # Veo 3.1 的 duration 必须是 '4s', '6s' 或 '8s' 格式（与 _parse_video_duration_seconds 已解析的秒数对齐）
        raw_d = _payload_get_duration_raw(payload)
        if raw_d is not None and raw_d != "":
            if isinstance(raw_d, str) and raw_d.strip().lower().endswith("s"):
                dur_str = raw_d.strip().lower()
                if dur_str in ("4s", "6s", "8s"):
                    out["duration"] = dur_str
                else:
                    out["duration"] = "6s"
            else:
                if duration_sec <= 4:
                    out["duration"] = "4s"
                elif duration_sec <= 6:
                    out["duration"] = "6s"
                else:
                    out["duration"] = "8s"
        else:
            out["duration"] = "6s"
        _ver = _sanitize_video_resolution_value(payload.get("resolution"))
        if _ver is not None:
            out["resolution"] = _ver
        if payload.get("generate_audio") is not None:
            out["generate_audio"] = bool(payload["generate_audio"])
        for k in ["seed", "negative_prompt"]:
            if k in payload:
                out[k] = payload[k]
        _merge_common_video_ui_fields(out, payload)
        return out

    # Grok Imagine Video：i2v 用 image_url，支持 duration
    if "grok" in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        _has_ar = _payload_get_aspect_ratio(payload) is not None
        if not first_url or _has_ar:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        out["duration"] = duration_sec
        _ger = _sanitize_video_resolution_value(payload.get("resolution"))
        if _ger is not None:
            out["resolution"] = _ger
        for k in ["audio", "seed", "negative_prompt"]:
            if k in payload:
                out[k] = payload[k]
        _merge_common_video_ui_fields(out, payload)
        return out

    # 即梦视频：只支持 prompt + image_url + end_image_url（无 duration/aspect_ratio）
    if "jimeng" in model.lower() or "即梦" in model:
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        if payload.get("end_image_url"):
            out["end_image_url"] = str(payload["end_image_url"])
        _merge_common_video_ui_fields(out, payload)
        return out

    # Seedance 1.0/1.5（非 v1/v1.5，即旧版本或特殊变体）：i2v 用 image_url，duration 字符串, aspect_ratio
    # 注意：这些版本可能也使用 options 对象，但为兼容性保留顶层参数
    if "seedance" in model.lower() and "/v1/" not in model.lower() and "/v1.5/" not in model.lower():
        out = {
            "model": model,
            "prompt": prompt,
            "duration": str(duration_sec),
            "aspect_ratio": aspect_ratio if ratio_ok else "16:9",
        }
        if first_url:
            out["image_url"] = first_url
        # 尝试使用 options 对象（如果模型支持）
        options: Dict[str, Any] = {}
        _sd2_res = _sanitize_video_resolution_value(payload.get("resolution"))
        if _sd2_res is not None:
            options["resolution"] = _sd2_res
        if payload.get("generate_audio") is not None:
            options["generate_audio"] = bool(payload.get("generate_audio"))
        if payload.get("camera_fixed") is not None:
            options["camera_fixed"] = bool(payload.get("camera_fixed"))
        if payload.get("seed") is not None:
            try:
                options["seed"] = int(payload.get("seed"))
            except (ValueError, TypeError):
                options["seed"] = payload.get("seed")
        if payload.get("end_image_url"):
            options["end_image_url"] = str(payload.get("end_image_url"))
        if payload.get("reference_image_urls"):
            options["reference_image_urls"] = payload.get("reference_image_urls")
        if payload.get("options") and isinstance(payload.get("options"), dict):
            options.update(payload.get("options"))
        _sanitize_options_dict_resolution(options)
        if options:
            out["options"] = options
        # 保留其他顶层参数（向后兼容）
        for k in ["audio", "negative_prompt"]:
            if k in payload and k not in options:
                out[k] = payload[k]
        _merge_common_video_ui_fields(out, payload)
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
    out["aspect_ratio"] = aspect_ratio
    out["duration"] = duration_sec

    # 统一处理图片 URL：优先使用 backend 注入的 filePaths/media_files
    if first_url and "image_url" not in out:
        out["image_url"] = first_url
    elif first_url:
        # 如果已有 image_url 但 backend 注入了新的，优先用新的
        out["image_url"] = first_url

    # 文生视频时，如果没有 aspect_ratio，添加默认值
    if not first_url and "aspect_ratio" not in out and aspect_ratio:
        out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"

    _fr = _sanitize_video_resolution_value(out.get("resolution"))
    if _fr is None:
        out.pop("resolution", None)
    else:
        out["resolution"] = _fr

    _merge_common_video_ui_fields(out, payload)
    return out


def _norm_json_key(k: Any) -> str:
    return str(k).replace("_", "").lower()


def _collect_xskill_public_url_fields_first(obj: Any, out: List[str], seen: set) -> None:
    """xskill tasks/query 的 data 内常见显式可访问字段 public_url（优先于同对象内其它 CDN 路径）。"""
    if isinstance(obj, dict):
        for k in ("public_url", "publicUrl"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith(("http://", "https://")) and v not in seen:
                seen.add(v)
                out.append(v.strip())
        for v in obj.values():
            _collect_xskill_public_url_fields_first(v, out, seen)
    elif isinstance(obj, list):
        for x in obj:
            _collect_xskill_public_url_fields_first(x, out, seen)


def _collect_xskill_result_primary_urls(obj: Any, out: List[str], seen: set) -> None:
    """完成态 result 对象内常见主链接（文档称结果在 result；优先于散落 image_url 正则顺序）。"""
    if isinstance(obj, dict):
        res = obj.get("result")
        if isinstance(res, dict):
            for k in ("url", "image_url", "video_url", "output_url"):
                v = res.get(k)
                if isinstance(v, str) and v.startswith(("http://", "https://")) and v not in seen:
                    seen.add(v)
                    out.append(v.strip())
        for v in obj.values():
            _collect_xskill_result_primary_urls(v, out, seen)
    elif isinstance(obj, list):
        for x in obj:
            _collect_xskill_result_primary_urls(x, out, seen)


def _reorder_cdn_urls_for_autosave(urls: List[str]) -> List[str]:
    """速推返回里常同时出现 TOS 长期链（…/assets/…）与任务直链（…/v3-tasks/…）。前者可稳定拉取，后者易不可访问；同列表内置后。"""
    assets: List[str] = []
    rest: List[str] = []
    v3tasks: List[str] = []
    seen: set = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        lu = u.lower()
        if "v3-tasks" in lu:
            v3tasks.append(u)
        elif "/assets/" in lu:
            assets.append(u)
        else:
            rest.append(u)
    return assets + rest + v3tasks


_TASK_AUTOSAVE_ONCE: Dict[str, float] = {}
_TASK_AUTOSAVE_TTL_SEC = 86400 * 2
_TASK_AUTOSAVE_MAX_KEYS = 50000


def _consume_task_autosave_once(task_id: str) -> bool:
    """同一 task_id 仅自动入库一次。get_result 在终态后仍会多次轮询，每次都会「终态成功」，否则会重复入库。"""
    tid = (task_id or "").strip()
    if not tid:
        return True
    now = time.time()
    dead = [k for k, t in _TASK_AUTOSAVE_ONCE.items() if now - t > _TASK_AUTOSAVE_TTL_SEC]
    for k in dead:
        del _TASK_AUTOSAVE_ONCE[k]
    if len(_TASK_AUTOSAVE_ONCE) > _TASK_AUTOSAVE_MAX_KEYS:
        _TASK_AUTOSAVE_ONCE.clear()
    if tid in _TASK_AUTOSAVE_ONCE:
        return False
    _TASK_AUTOSAVE_ONCE[tid] = now
    return True


_TASK_RESULT_BILL_ONCE: Dict[str, float] = {}
_TASK_RESULT_BILL_TTL_SEC = 86400 * 7
_TASK_RESULT_BILL_MAX_KEYS = 50000


def _consume_task_result_bill_once(task_id: str) -> bool:
    """同一 task_id 的 get_result 终态成功会多次轮询：消耗结算只执行一次，避免重复扣 lobster 用户积分。"""
    tid = (task_id or "").strip()
    if not tid:
        return True
    now = time.time()
    dead = [k for k, t in _TASK_RESULT_BILL_ONCE.items() if now - t > _TASK_RESULT_BILL_TTL_SEC]
    for k in dead:
        del _TASK_RESULT_BILL_ONCE[k]
    if len(_TASK_RESULT_BILL_ONCE) > _TASK_RESULT_BILL_MAX_KEYS:
        _TASK_RESULT_BILL_ONCE.clear()
    if tid in _TASK_RESULT_BILL_ONCE:
        return False
    _TASK_RESULT_BILL_ONCE[tid] = now
    return True


def _prefer_stable_urls_for_autosave(urls: List[str]) -> List[str]:
    """同一结果里若同时有 mcp-images 临时链与 v3-tasks/assets 链，只保留后者，避免同一张图入两条素材。"""
    if not urls:
        return []
    has_stable = any(
        ("v3-tasks" in u.lower()) or ("/assets/" in u)
        for u in urls
    )
    if not has_stable:
        return urls
    out = [u for u in urls if "mcp-images" not in u.lower()]
    return out if out else urls


def _extract_media_urls_for_auto_save(upstream_resp: Any) -> List[str]:
    """从上游 JSON 提取媒体 URL：带扩展名正则 + 常见字段递归（无扩展名 CDN 直链）。"""
    order: List[str] = []
    seen: set = set()
    if isinstance(upstream_resp, (dict, list)):
        _collect_xskill_public_url_fields_first(upstream_resp, order, seen)
        _collect_xskill_result_primary_urls(upstream_resp, order, seen)
    blob = (
        json.dumps(_sanitize_for_json(upstream_resp), ensure_ascii=False)
        if isinstance(upstream_resp, (dict, list))
        else str(upstream_resp)
    )
    for m in _MEDIA_URL_RE.findall(blob):
        if m not in seen:
            seen.add(m)
            order.append(m)

    def maybe_add(u: str) -> None:
        u = (u or "").strip()
        if len(u) < 16 or not u.startswith(("http://", "https://")):
            return
        if u not in seen:
            seen.add(u)
            order.append(u)

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                nk = _norm_json_key(k)
                if isinstance(v, str):
                    if nk in (
                        "imageurl", "videourl", "mediaurl", "outputurl", "fileurl", "resulturl",
                        "thumbnailurl", "coverurl", "downloadurl", "previewurl", "publicurl", "persistenturl",
                        "src", "href", "image",
                    ) or nk.endswith("url"):
                        maybe_add(v)
                walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    if isinstance(upstream_resp, (dict, list)):
        walk(upstream_resp)
    return _reorder_cdn_urls_for_autosave(order)[:12]


_NO_AUTO_SAVE_CAPABILITIES = frozenset({
    "sutui.search_models",
    "sutui.guide",
    "sutui.transfer_url",
})

async def _auto_save_generated_assets(
    upstream_resp: Any, capability_id: str, payload: Dict, token: Optional[str],
    request: Optional[Request] = None,
) -> List[Dict[str, str]]:
    """Extract media URLs from upstream result and auto-save as local assets."""
    if not token:
        return []
    if capability_id in _NO_AUTO_SAVE_CAPABILITIES:
        return []
    urls = _prefer_stable_urls_for_autosave(_extract_media_urls_for_auto_save(upstream_resp))
    if not urls:
        return []

    prompt_text = payload.get("prompt", "") or capability_id

    def _mt_for_url(u: str) -> str:
        """先按 URL 路径扩展名区分，避免视频任务里缩略图/封面 .jpg 被标成 video（save-url 会把图强行当 mp4 扩展名，预览坏）。"""
        path = (u or "").split("?")[0].split("#")[0].lower()
        if path.endswith((".mp4", ".webm", ".mov")):
            return "video"
        if path.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            return "image"
        if capability_id.startswith("video") or "video" in capability_id:
            return "video"
        if capability_id == "task.get_result" and payload.get("capability_id"):
            cid = str(payload.get("capability_id") or "")
            if cid.startswith("video"):
                return "video"
        return "image"

    saved: List[Dict[str, str]] = []
    for url in urls[:8]:
        mt = _mt_for_url(url)
        body = {
            "url": url,
            "media_type": mt,
            "prompt": prompt_text[:500],
            "tags": f"auto,{capability_id}",
        }
        try:
            async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
                r = await client.post(f"{BASE_URL}/api/assets/save-url", json=body, headers=_backend_headers(token, request))
            if r.status_code < 400:
                d = r.json()
                item: Dict[str, str] = {
                    "asset_id": d.get("asset_id", ""),
                    "filename": d.get("filename", ""),
                    "media_type": mt,
                }
                su = d.get("source_url")
                if su:
                    item["source_url"] = str(su)
                saved.append(item)
            else:
                logger.warning(
                    "[MCP auto_save] save-url HTTP %s url_prefix=%s body_prefix=%s",
                    r.status_code,
                    (url[:96] + "…") if len(url) > 96 else url,
                    (r.text or "")[:240],
                )
        except Exception as e:
            logger.warning("[MCP auto_save] save-url 异常: %s url_prefix=%s", e, (url[:96] + "…") if len(url) > 96 else url)
    return saved




async def _call_tool(name: str, args: Dict[str, Any], token: Optional[str], request: Optional[Request] = None) -> Tuple[List[Dict[str, Any]], bool]:
    try:
        catalog = _load_capability_catalog()
        upstream_urls = _load_upstream_urls()

        if name == "list_capabilities":
            is_admin = await _fetch_is_skill_store_admin(token)
            allowed = await _fetch_user_allowed_capability_ids(token)
            _comfly_image_models: list[str] = []
            _comfly_video_models: list[str] = []
            try:
                from comfly_upstream import _load_pricing as _lp
                _cp = _lp()
                for _mk, _mv in (_cp.get("models") or {}).items():
                    if not isinstance(_mv, dict):
                        continue
                    _fmt = _mv.get("api_format", "")
                    if _fmt == "dalle":
                        _comfly_image_models.append(_mk)
                    elif _fmt in ("unified_video",):
                        _comfly_video_models.append(_mk)
            except Exception:
                pass
            caps_out = []
            for cid in sorted(catalog.keys()):
                if catalog[cid].get("enabled") is False:
                    continue
                if _capability_id_is_debug_only_in_registry(cid) and not is_admin:
                    continue
                if allowed is not None and cid not in allowed:
                    continue
                desc = catalog[cid].get("description") or cid
                if cid == "image.generate" and _comfly_image_models:
                    desc += f"  可用模型包括: {', '.join(_comfly_image_models)}（用户指定模型时必须原样传入 model 字段）"
                elif cid == "video.generate" and _comfly_video_models:
                    desc += f"  可用模型包括: {', '.join(_comfly_video_models)}（用户指定模型时必须原样传入 model 字段）"
                caps_out.append({"capability_id": cid, "description": desc})
            data = {"capabilities": caps_out}
            return [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}], False

        if name == "manage_skills":
            action = str(args.get("action") or "").strip()
            package_id = str(args.get("package_id") or "").strip()
            query = str(args.get("query") or "").strip()
            mcp_name = str(args.get("name") or "").strip()
            mcp_url = str(args.get("url") or "").strip()

            if action == "search_online":
                if not query:
                    return [{"type": "text", "text": "请提供 query 参数，如 'image', 'database', 'github'"}], True
                async with httpx.AsyncClient(timeout=60.0) as client:
                    # Browse a few pages first to populate cache
                    for pg in range(1, 4):
                        await client.get(
                            f"{BASE_URL}/api/mcp-registry/browse",
                            params={"page": str(pg)},
                            headers=_backend_headers(token, request),
                        )
                    # Now search the cache
                    r = await client.get(
                        f"{BASE_URL}/api/mcp-registry/search",
                        params={"q": query, "page_size": "20"},
                        headers=_backend_headers(token, request),
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
                        headers=_backend_headers(token, request),
                    )
                return [{"type": "text", "text": json.dumps(r.json() if r.content else {}, ensure_ascii=False, indent=2)}], r.status_code >= 400

            async with httpx.AsyncClient(timeout=30.0) as client:
                if action == "list_store":
                    r = await client.get(f"{BASE_URL}/skills/store", headers=_backend_headers(token, request))
                elif action == "list_installed":
                    r = await client.get(f"{BASE_URL}/skills/installed", headers=_backend_headers(token, request))
                elif action == "install":
                    if not package_id:
                        return [{"type": "text", "text": "请提供 package_id"}], True
                    r = await client.post(f"{BASE_URL}/skills/install", json={"package_id": package_id}, headers=_backend_headers(token, request))
                elif action == "uninstall":
                    if not package_id:
                        return [{"type": "text", "text": "请提供 package_id"}], True
                    r = await client.post(f"{BASE_URL}/skills/uninstall", json={"package_id": package_id}, headers=_backend_headers(token, request))
                else:
                    return [{"type": "text", "text": f"未知操作: {action}。支持: list_store, list_installed, install, uninstall, search_online, add_mcp"}], True
            return [{"type": "text", "text": json.dumps(r.json() if r.content else {}, ensure_ascii=False, indent=2)}], r.status_code >= 400

        if name == "invoke_capability":
            capability_id = str(args.get("capability_id") or "").strip()
            payload = args.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            if capability_id == "publish_content":
                logger.warning(
                    "[MCP] invoke_capability 误用 capability_id=publish_content，已转发为 publish_content"
                )
                pub_args: Dict[str, Any] = dict(payload)
                for k in (
                    "asset_id",
                    "account_nickname",
                    "account_id",
                    "title",
                    "description",
                    "tags",
                    "options",
                    "cover_asset_id",
                    "platform",
                ):
                    if k in args and k not in ("capability_id", "payload"):
                        v = args[k]
                        if v is None:
                            continue
                        cur = pub_args.get(k)
                        if cur is None or (isinstance(cur, str) and not cur.strip()):
                            pub_args[k] = v
                return await _call_tool("publish_content", pub_args, token, request)
            if not capability_id or capability_id not in catalog:
                return [{"type": "text", "text": f"能力未找到: {capability_id}"}], True

            if not (token or "").strip():
                return [
                    {
                        "type": "text",
                        "text": (
                            "调用能力需要登录：请在 MCP 请求中携带 Authorization: Bearer（用户 JWT），"
                            "以便预扣积分与结算；匿名请求不会转发上游。"
                        ),
                    }
                ], True
            user_brand_mark = resolve_brand_mark_for_request(
                (request.headers.get("Authorization") or request.headers.get("x-user-authorization") or "")
            )
            if _capability_id_is_debug_only_in_registry(capability_id) and not await _fetch_is_skill_store_admin(token):
                return [{"type": "text", "text": "该能力为调试中技能，当前账号不可用。"}], True
            allowed = await _fetch_user_allowed_capability_ids(token)
            if allowed is not None and capability_id not in allowed:
                return [{"type": "text", "text": f"当前账号未开通此能力: {capability_id}"}], True
            cfg = catalog[capability_id]
            upstream_tool = str(cfg.get("upstream_tool") or "").strip()
            if not upstream_tool:
                return [{"type": "text", "text": f"能力配置缺失 upstream_tool: {capability_id}"}], True
            upstream_name = str(cfg.get("upstream") or "sutui").strip()
            upstream_url = upstream_urls.get(upstream_name, "").strip()
            if upstream_name == "sutui":
                _bm = (user_brand_mark or "").strip().lower()
                if _bm not in ("bihuo", "yingshi"):
                    return [
                        {
                            "type": "text",
                            "text": (
                                "当前账号未绑定必火(bihuo)或影视(yingshi)品牌，无法使用速推算力；"
                                "无通用兜底。请使用对应品牌安装包注册/登录，或联系管理员写入 brand_mark 后重新登录。"
                            ),
                        }
                    ], True
            sutui_token: Optional[str] = None
            sutui_pool_for_billing = ""
            sutui_token_ref_for_billing = ""
            if upstream_name == "sutui":
                sutui_token, sutui_pool_for_billing = await next_sutui_server_token_with_pool(
                    brand_mark=user_brand_mark
                )
                if not sutui_token:
                    return [
                        {
                            "type": "text",
                            "text": (
                                "速推 Token 未配置或当前品牌池为空。请配置 SUTUI_SERVER_TOKENS_BIHUO / "
                                "SUTUI_SERVER_TOKENS_YINGSHI 后重试。"
                            ),
                        }
                    ], True
                sutui_token_ref_for_billing = sutui_token_ref_from_secret(sutui_token)
            # 先规范化 payload（与上游一致），再按速推官方 docs 定价预扣积分
            original_model = payload.get("model") if isinstance(payload, dict) else None
            if capability_id == "image.generate":
                try:
                    payload = _normalize_image_generate_payload(payload)
                except ValueError as e:
                    return [{"type": "text", "text": f"image.generate 参数错误: {e}"}], True
            elif capability_id == "video.generate":
                try:
                    payload = _normalize_video_generate_payload(payload)
                except ValueError as e:
                    return [{"type": "text", "text": f"video.generate 参数错误: {e}"}], True
            elif capability_id == "image.understand":
                payload = _normalize_understand_payload(payload, media_key="image_urls", default_model="openrouter/router/vision")
            elif capability_id == "video.understand":
                payload = _normalize_understand_payload(payload, media_key="video_urls", default_model="openrouter/router/video")

            if capability_id in ("image.generate", "video.generate"):
                _mid = (payload.get("model") or payload.get("model_id") or "").strip()
                _cap_mismatch = hint_for_wrong_capability_model(capability_id, _mid)
                if _cap_mismatch:
                    return [{"type": "text", "text": _cap_mismatch}], True

            normalized_model = payload.get("model") if isinstance(payload, dict) else None
            if original_model != normalized_model:
                logger.info("[MCP] 模型名称映射: %s -> %s", original_model, normalized_model)

            record_capability_id = capability_id
            if capability_id == "task.get_result" and isinstance(payload, dict):
                _emb = (payload.get("capability_id") or "").strip()
                if _emb in ("video.generate", "image.generate"):
                    record_capability_id = _emb

            # ━━━ Comfly 路由预判（在预扣之前决定，以便正确计价） ━━━
            _early_use_comfly = False
            _early_comfly_task_query = False
            _comfly_model_id = (normalized_model or original_model or "").strip()
            _payload_prefer_comfly = bool(payload.get("_prefer_comfly")) if isinstance(payload, dict) else False
            _comfly_user_credits: Optional[int] = None
            try:
                from comfly_upstream import (
                    should_route_to_comfly as _early_should_cf,
                    is_comfly_task as _early_is_cf_task,
                    is_comfly_configured as _early_cf_ok,
                    estimate_comfly_credits as _early_est_cf,
                )
                if capability_id == "comfly.chat":
                    _early_use_comfly = _early_cf_ok()
                elif capability_id == "task.get_result":
                    _poll_tid_early = str(payload.get("task_id") or "").strip()
                    if _poll_tid_early and _early_is_cf_task(_poll_tid_early):
                        _early_comfly_task_query = True
                        _early_use_comfly = True
                elif _payload_prefer_comfly and _early_cf_ok() and capability_id in ("image.generate", "video.generate"):
                    _early_use_comfly = True
                else:
                    _early_use_comfly = _early_should_cf(capability_id, _comfly_model_id)
                logger.info(
                    "[MCP] Comfly路由预判 capability=%s model=%s use_comfly=%s configured=%s prefer=%s",
                    capability_id, _comfly_model_id, _early_use_comfly, _early_cf_ok(), _payload_prefer_comfly,
                )
                if _early_use_comfly and not _early_comfly_task_query:
                    _comfly_user_credits = _early_est_cf(_comfly_model_id, payload if isinstance(payload, dict) else {}, for_user=True)
            except Exception as _cf_early_err:
                logger.warning("[MCP] Comfly 路由预判跳过: %s", _cf_early_err)

            # ━━━ 用户积分：唯一业务入口（调用上游之前只在此处处理） ━━━
            pre_deduct_amount = quantize_credits(0)
            billing_idem = str(uuid.uuid4())
            if token:
                try:
                    pre_body: Dict[str, Any] = {"capability_id": capability_id}
                    _UNDERSTAND_CAPS = ("image.understand", "video.understand")
                    if _early_use_comfly and _comfly_user_credits and _comfly_user_credits > 0:
                        pre_body["force_credits"] = _comfly_user_credits
                        pre_body["model"] = _comfly_model_id
                    elif upstream_name == "sutui" and upstream_tool == "generate" and capability_id not in _UNDERSTAND_CAPS:
                        pre_body["model"] = (payload.get("model") or "").strip()
                        pre_body["params"] = payload
                    if upstream_name == "sutui" and sutui_pool_for_billing and sutui_token_ref_for_billing:
                        pre_body["sutui_pool"] = sutui_pool_for_billing
                        pre_body["sutui_token_ref"] = sutui_token_ref_for_billing
                    _pre_hdr = dict(_backend_headers(token, request))
                    _pre_hdr["X-Billing-Idempotency-Key"] = billing_idem
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        pre_r = await client.post(
                            f"{_capabilities_api_base()}/capabilities/pre-deduct",
                            json=_sanitize_for_json(pre_body),
                            headers=_pre_hdr,
                        )
                    if pre_r.status_code == 400:
                        raw = pre_r.json() if pre_r.content else {}
                        detail = raw.get("detail", "无法预扣积分")
                        if isinstance(detail, list):
                            detail = str(detail)
                        return [{"type": "text", "text": str(detail)}], True
                    if pre_r.status_code == 402:
                        detail = (pre_r.json() or {}).get("detail", "积分不足")
                        d = str(detail or "").strip()
                        base = "当前账户积分不足，无法调用该能力。请前往「充值」或积分页购买/充值后再试。"
                        msg = base if (not d or d in ("积分不足", "余额不足")) else f"{base}（{d}）"
                        return [{"type": "text", "text": msg}], True
                    if pre_r.status_code == 200:
                        try:
                            body_ok = pre_r.json() if pre_r.content else {}
                            if not isinstance(body_ok, dict):
                                body_ok = {}
                            pre_deduct_amount = quantize_credits(body_ok.get("credits_charged") or 0)
                        except Exception as parse_e:
                            logger.warning(
                                "[MCP] pre_deduct 200 响应非 JSON capability_id=%s err=%s body_prefix=%s",
                                capability_id,
                                parse_e,
                                (pre_r.text or "")[:300],
                            )
                            return [
                                {
                                    "type": "text",
                                    "text": "预扣积分返回异常（无法解析认证中心响应）。请稍后重试。",
                                }
                            ], True
                except Exception as e:
                    if upstream_name == "sutui" and upstream_tool == "generate":
                        logger.exception("[MCP] pre-deduct 请求失败 capability_id=%s", capability_id)
                        return [
                            {
                                "type": "text",
                                "text": (
                                    "无法连接认证中心完成预扣积分（网络或超时）。请稍后重试。"
                                    f" 详情：{type(e).__name__}: {str(e)[:200]}"
                                ),
                            }
                        ], True

            if not upstream_url:
                return [{"type": "text", "text": f"未配置上游网关: {upstream_name}，请在 .env 或技能商店中配置"}], True

            # 检测并转存内部图片 URL 到公开 CDN（图生视频/图生图需要）
            temp_ids_to_register = []  # 在外部作用域定义，用于后续注册
            if capability_id in ("image.generate", "video.generate") and isinstance(payload, dict):
                # 收集所有可能的图片 URL（从 image_url、filePaths、media_files）
                urls_to_check = []
                image_url = payload.get("image_url") or ""
                if image_url and isinstance(image_url, str):
                    urls_to_check.append(("image_url", image_url.strip()))
                
                file_paths = payload.get("filePaths") or []
                if isinstance(file_paths, list):
                    for idx, fp in enumerate(file_paths):
                        if isinstance(fp, str) and fp.strip():
                            urls_to_check.append((f"filePaths[{idx}]", fp.strip()))
                
                media_files = payload.get("media_files") or []
                if isinstance(media_files, list):
                    for idx, mf in enumerate(media_files):
                        if isinstance(mf, str) and mf.strip():
                            urls_to_check.append((f"media_files[{idx}]", mf.strip()))
                
                # 提取临时文件ID并注册（用于任务完成后清理）
                for url_key, url_value in urls_to_check:
                    if "/api/assets/temp/" in url_value:
                        try:
                            from urllib.parse import urlparse
                            parsed = urlparse(url_value)
                            path_parts = parsed.path.split("/")
                            if "temp" in path_parts:
                                temp_idx = path_parts.index("temp")
                                if temp_idx + 1 < len(path_parts):
                                    temp_id = path_parts[temp_idx + 1].split("?")[0]
                                    if temp_id.startswith("temp_"):
                                        temp_ids_to_register.append(temp_id)
                        except Exception:
                            pass
                
                # 对每个 URL 进行检测和转存
                for url_key, url_value in urls_to_check:
                    if not url_value:
                        continue
                    
                    # 检测是否是内部地址（需要转存）
                    is_internal = False
                    try:
                        from urllib.parse import urlparse
                        import ipaddress
                        parsed = urlparse(url_value)
                        hostname = (parsed.hostname or "").lower()
                        # 内部地址检测：localhost、127.0.0.1、内网 IP、api.51ins.com 等
                        if not hostname:
                            is_internal = True
                        elif hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
                            is_internal = True
                        elif "api.51ins.com" in hostname or "bhzn.top" in hostname or "42.194.209.150" in hostname:
                            is_internal = True
                        else:
                            # 尝试解析为 IP 地址，判断是否为内网 IP
                            try:
                                ip = ipaddress.ip_address(hostname)
                                if ip.is_private or ip.is_loopback:
                                    is_internal = True
                            except ValueError:
                                # 不是 IP 地址，检查是否是已知的公开 CDN
                                # 公开 CDN 通常包含这些关键词，认为是公开的
                                cdn_keywords = ("cdn.", "oss.", "cos.", "tos.", "s3.", "cloudfront.", "fastly.", "cloudflare.", "img.", "static.", "media.", "assets.", "qiniucdn.", "upyun.", "aliyuncs.")
                                if not any(cdn_keyword in hostname for cdn_keyword in cdn_keywords):
                                    # 如果包含 token 参数，可能是内部 API，需要转存
                                    if "token=" in url_value or "?token" in url_value:
                                        is_internal = True
                    except Exception:
                        pass
                    
                    # 如果是内部地址，自动转存到公开 CDN
                    if is_internal and upstream_name == "sutui" and upstream_url:
                        cdn_url = None
                        # 【服务器端MCP-步骤C.5】方法1：尝试使用 TOS 转存（如果配置了 TOS）
                        try:
                            from backend.app.api.assets import _get_tos_config, _upload_to_tos
                            logger.info("[服务器端MCP-步骤C.5] 检查服务器端TOS配置 url_key=%s", url_key)
                            tos_cfg = _get_tos_config()
                            if tos_cfg:
                                logger.info("[服务器端MCP-步骤C.5.1] 服务器端TOS配置存在，开始下载内部图片 url_key=%s url_value=%s", url_key, url_value[:100])
                                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                                    resp = await client.get(url_value)
                                    logger.info("[服务器端MCP-步骤C.5.2] 下载响应 status=%d url_key=%s", resp.status_code, url_key)
                                    resp.raise_for_status()
                                    data = resp.content
                                    content_type = resp.headers.get("content-type", "image/jpeg")
                                    logger.info("[服务器端MCP-步骤C.5.3] 下载成功 size=%d content_type=%s url_key=%s", len(data), content_type, url_key)
                                    # 从 URL 推断扩展名
                                    from urllib.parse import urlparse
                                    from pathlib import Path
                                    parsed = urlparse(url_value)
                                    path = Path(parsed.path)
                                    ext = path.suffix.lower() if path.suffix else ".jpg"
                                    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                                        ext = ".jpg"
                                    object_key = f"mcp-transfer/{uuid.uuid4().hex[:12]}{ext}"
                                    logger.info("[服务器端MCP-步骤C.5.4] 开始上传到TOS object_key=%s url_key=%s", object_key, url_key)
                                    cdn_url = _upload_to_tos(data, object_key, content_type)
                                    if cdn_url:
                                        # 更新对应的字段
                                        if url_key == "image_url":
                                            payload["image_url"] = cdn_url
                                        elif url_key.startswith("filePaths["):
                                            idx = int(url_key.split("[")[1].split("]")[0])
                                            if isinstance(payload.get("filePaths"), list):
                                                payload["filePaths"][idx] = cdn_url
                                        elif url_key.startswith("media_files["):
                                            idx = int(url_key.split("[")[1].split("]")[0])
                                            if isinstance(payload.get("media_files"), list):
                                                payload["media_files"][idx] = cdn_url
                                        logger.info("[服务器端MCP-步骤C.5.5] TOS转存成功 url_key=%s 原URL=%s CDN_URL=%s", url_key, url_value[:80], cdn_url[:80])
                                    else:
                                        logger.warning("[服务器端MCP-步骤C.5.5] TOS转存失败 url_key=%s", url_key)
                        except httpx.HTTPStatusError as e:
                            logger.error("[服务器端MCP-步骤C.5.2] 下载失败 HTTP错误 url_key=%s status=%d url=%s", url_key, e.response.status_code, url_value[:100])
                        except Exception as e:
                            logger.error("[服务器端MCP-步骤C.5] TOS转存过程异常 url_key=%s error=%s", url_key, str(e), exc_info=True)
                        
                        # 【服务器端MCP-步骤C.6】方法2：如果 TOS 转存失败，使用 sutui.transfer_url
                        if not cdn_url:
                            logger.info("[服务器端MCP-步骤C.6] TOS转存未成功，尝试使用sutui.transfer_url url_key=%s", url_key)
                            try:
                                logger.info("[服务器端MCP-步骤C.6.1] 调用sutui.transfer_url url_key=%s url_value=%s", url_key, url_value[:100])
                                transfer_resp = await _call_upstream_mcp_tool(
                                    upstream_url,
                                    "transfer_url",
                                    {"url": url_value, "type": "image"},
                                    upstream_name=upstream_name,
                                    sutui_token=sutui_token,
                                    lobster_capability_id=capability_id,
                                    brand_mark=user_brand_mark,
                                )
                                logger.info("[服务器端MCP-步骤C.6.2] sutui.transfer_url 调用完成 url_key=%s", url_key)
                                if isinstance(transfer_resp, dict):
                                    err_obj = transfer_resp.get("error")
                                    if not err_obj:
                                        # 解析转存后的 URL
                                        # 记录完整响应以便调试（info 级别，方便排查问题）
                                        logger.info("[服务器端MCP-步骤C.6.3] sutui.transfer_url 完整响应 url_key=%s response=%s", url_key, json.dumps(_sanitize_for_json(transfer_resp), ensure_ascii=False, indent=2)[:800])
                                        
                                        # 尝试多种解析方式
                                        cdn_url = None
                                        
                                        # 方式1：从 result.content[].text 解析 JSON
                                        content = transfer_resp.get("result", {}).get("content", [])
                                        if isinstance(content, list):
                                            for item in content:
                                                if isinstance(item, dict) and item.get("type") == "text":
                                                    text = item.get("text", "")
                                                    try:
                                                        transfer_data = json.loads(text) if text else {}
                                                        # 检查是否转存失败
                                                        if transfer_data.get("success") is False:
                                                            error_msg = transfer_data.get("error", "未知错误")
                                                            logger.error("[服务器端MCP-步骤C.6.3] sutui.transfer_url 转存失败 url_key=%s error=%s URL=%s", url_key, error_msg, url_value[:80])
                                                            break
                                                        # 尝试多种可能的字段名
                                                        cdn_url = (
                                                            transfer_data.get("url") or 
                                                            transfer_data.get("cdn_url") or 
                                                            transfer_data.get("transfer_url") or
                                                            transfer_data.get("public_url") or
                                                            transfer_data.get("data", {}).get("url") if isinstance(transfer_data.get("data"), dict) else None
                                                        )
                                                        if cdn_url and isinstance(cdn_url, str) and cdn_url.startswith("http"):
                                                            # 更新对应的字段
                                                            if url_key == "image_url":
                                                                payload["image_url"] = cdn_url
                                                            elif url_key.startswith("filePaths["):
                                                                idx = int(url_key.split("[")[1].split("]")[0])
                                                                if isinstance(payload.get("filePaths"), list):
                                                                    payload["filePaths"][idx] = cdn_url
                                                            elif url_key.startswith("media_files["):
                                                                idx = int(url_key.split("[")[1].split("]")[0])
                                                                if isinstance(payload.get("media_files"), list):
                                                                    payload["media_files"][idx] = cdn_url
                                                            logger.info("[服务器端MCP-步骤C.6.3] sutui.transfer_url 转存成功（方式1：从content解析）url_key=%s 原URL=%s CDN_URL=%s", url_key, url_value[:80], cdn_url[:80])
                                                            break
                                                    except json.JSONDecodeError:
                                                        # 如果不是 JSON，可能是直接的 URL 字符串
                                                        if text.strip().startswith("http"):
                                                            cdn_url = text.strip()
                                                            # 更新对应的字段
                                                            if url_key == "image_url":
                                                                payload["image_url"] = cdn_url
                                                            elif url_key.startswith("filePaths["):
                                                                idx = int(url_key.split("[")[1].split("]")[0])
                                                                if isinstance(payload.get("filePaths"), list):
                                                                    payload["filePaths"][idx] = cdn_url
                                                            elif url_key.startswith("media_files["):
                                                                idx = int(url_key.split("[")[1].split("]")[0])
                                                                if isinstance(payload.get("media_files"), list):
                                                                    payload["media_files"][idx] = cdn_url
                                                            logger.info("[服务器端MCP-步骤C.6.3] sutui.transfer_url 转存成功（方式1：直接URL字符串）url_key=%s 原URL=%s CDN_URL=%s", url_key, url_value[:80], cdn_url[:80])
                                                            break
                                                    except Exception as e:
                                                        logger.warning("[服务器端MCP-步骤C.6.3] 解析 transfer_url 响应项失败 url_key=%s error=%s", url_key, str(e))
                                        
                                        # 方式2：直接从 result 中取 URL（某些 MCP 可能直接返回）
                                        if not cdn_url:
                                            result = transfer_resp.get("result", {})
                                            if isinstance(result, dict):
                                                cdn_url = (
                                                    result.get("url") or 
                                                    result.get("cdn_url") or 
                                                    result.get("transfer_url") or
                                                    result.get("data", {}).get("url") if isinstance(result.get("data"), dict) else None
                                                )
                                                if cdn_url and isinstance(cdn_url, str) and cdn_url.startswith("http"):
                                                    # 更新对应的字段
                                                    if url_key == "image_url":
                                                        payload["image_url"] = cdn_url
                                                    elif url_key.startswith("filePaths["):
                                                        idx = int(url_key.split("[")[1].split("]")[0])
                                                        if isinstance(payload.get("filePaths"), list):
                                                            payload["filePaths"][idx] = cdn_url
                                                    elif url_key.startswith("media_files["):
                                                        idx = int(url_key.split("[")[1].split("]")[0])
                                                        if isinstance(payload.get("media_files"), list):
                                                            payload["media_files"][idx] = cdn_url
                                                    logger.info("[服务器端MCP-步骤C.6.3] sutui.transfer_url 转存成功（方式2：从result解析）url_key=%s 原URL=%s CDN_URL=%s", url_key, url_value[:80], cdn_url[:80])
                                        
                                        if not cdn_url:
                                            logger.error("[服务器端MCP-步骤C.6.4] sutui.transfer_url 返回成功但无法解析 CDN URL url_key=%s 完整响应=%s", url_key, json.dumps(_sanitize_for_json(transfer_resp), ensure_ascii=False, indent=2)[:800])
                                        else:
                                            # 验证转存后的 URL 是否可访问（简单检查格式）
                                            if not (cdn_url.startswith("http://") or cdn_url.startswith("https://")):
                                                logger.error("[服务器端MCP-步骤C.6.4] sutui.transfer_url 返回的 URL 格式异常（非 http/https）url_key=%s url=%s", url_key, cdn_url[:200])
                                                cdn_url = None  # 重置，让 TOS 或其他方式处理
                                    else:
                                        logger.error("[服务器端MCP-步骤C.6.2] sutui.transfer_url 返回错误 url_key=%s error=%s", url_key, err_obj.get("message", ""))
                            except Exception as e:
                                logger.error("[服务器端MCP-步骤C.6] sutui.transfer_url 调用异常 url_key=%s error=%s", url_key, str(e), exc_info=True)
                        
                        if not cdn_url:
                            logger.error("[服务器端MCP-步骤C.7] 所有转存方式都失败，将使用原URL（可能无法访问）url_key=%s url_value=%s", url_key, url_value[:100])
                        else:
                            logger.info("[服务器端MCP-步骤C.7] 转存成功 url_key=%s 原URL=%s CDN_URL=%s", url_key, url_value[:80], cdn_url[:80])
            
            # ━━━ Comfly 路由：使用预判结果 ━━━
            _use_comfly = _early_use_comfly
            _comfly_task_query = _early_comfly_task_query

            t0 = time.perf_counter()
            if _use_comfly:
                logger.info("[MCP] invoke_capability capability_id=%s upstream=comfly model=%s task_query=%s", capability_id, _comfly_model_id, _comfly_task_query)
                try:
                    from comfly_upstream import (
                        call_comfly_image_generate,
                        call_comfly_video_generate,
                        call_comfly_task_query,
                        call_comfly_chat_completions,
                        format_comfly_image_response_as_sutui,
                        format_comfly_video_response_as_sutui,
                        register_comfly_task,
                        get_comfly_task_token_group,
                        _get_model_token_group,
                    )
                    if _comfly_task_query:
                        _poll_tid = str(payload.get("task_id") or "").strip()
                        _cf_resp = await call_comfly_task_query(_poll_tid, get_comfly_task_token_group(_poll_tid))
                        upstream_resp = format_comfly_video_response_as_sutui(_cf_resp)
                    elif capability_id == "comfly.chat":
                        _cf_model = (payload.get("model") or "").strip()
                        _cf_messages = payload.get("messages") or []
                        _cf_resp = await call_comfly_chat_completions(
                            _cf_model,
                            _cf_messages,
                            temperature=float(payload.get("temperature", 0.7)),
                            max_tokens=payload.get("max_tokens"),
                        )
                        if _cf_resp.get("error"):
                            upstream_resp = _cf_resp
                        else:
                            _choices = _cf_resp.get("choices") or []
                            _reply = ""
                            if _choices:
                                _msg = _choices[0].get("message") or {}
                                _reply = _msg.get("content") or ""
                            upstream_resp = {
                                "result": _reply,
                                "usage": _cf_resp.get("usage"),
                                "_comfly": True,
                            }
                    elif capability_id == "image.generate":
                        _cf_resp = await call_comfly_image_generate(_comfly_model_id, payload)
                        upstream_resp = format_comfly_image_response_as_sutui(_cf_resp)
                    elif capability_id == "video.generate":
                        _cf_resp = await call_comfly_video_generate(_comfly_model_id, payload)
                        upstream_resp = format_comfly_video_response_as_sutui(_cf_resp)
                        _cf_tid = (upstream_resp.get("task_id") or "") if isinstance(upstream_resp, dict) else ""
                        if _cf_tid:
                            register_comfly_task(_cf_tid, _get_model_token_group(_comfly_model_id))
                    else:
                        upstream_resp = {"error": {"message": f"Comfly 不支持 {capability_id}"}}
                except Exception as _cf_call_err:
                    logger.exception("[MCP] Comfly 调用异常 capability_id=%s", capability_id)
                    upstream_resp = {"error": {"message": f"Comfly 调用失败: {_cf_call_err}"}}
            else:
                logger.info("[MCP] invoke_capability capability_id=%s upstream=%s model=%s", capability_id, upstream_name, normalized_model or original_model or "(无)")
                upstream_resp = await _call_upstream_mcp_tool(
                    upstream_url,
                    upstream_tool,
                    payload,
                    upstream_name=upstream_name,
                    sutui_token=sutui_token,
                    lobster_capability_id=capability_id,
                    brand_mark=user_brand_mark,
                )
            # task.get_result: 不再在此处轮询，由 backend chat 每 15s 轮询并写回对话
            latency_ms = int((time.perf_counter() - t0) * 1000)
            upstream_error = ""
            if isinstance(upstream_resp, dict):
                err_obj = upstream_resp.get("error")
                if isinstance(err_obj, dict):
                    upstream_error = str(err_obj.get("message") or "")[:500]
            poll_task_id = str(payload.get("task_id") or payload.get("taskId") or "").strip()
            create_billed_amount_peek = quantize_credits(0)
            if upstream_tool == "get_result" and poll_task_id:
                create_billed_amount_peek = _peek_task_billed_credits(poll_task_id)

            # 如果是video.generate调用，从响应中提取task_id并注册临时文件
            if capability_id == "video.generate" and isinstance(upstream_resp, dict):
                # 从响应中提取task_id
                generated_task_id = _extract_task_id_from_sutui_response(upstream_resp)
                if generated_task_id and temp_ids_to_register:
                    try:
                        from backend.app.api.assets import register_temp_file_for_task
                        for temp_id in temp_ids_to_register:
                            register_temp_file_for_task(generated_task_id, temp_id)
                            logger.info("[临时文件] 注册 task_id=%s temp_id=%s", generated_task_id, temp_id)
                    except Exception as e:
                        logger.debug("[临时文件] 注册失败 error=%s", e)
                # 清空临时ID列表，避免重复注册
                temp_ids_to_register.clear()
            # get_result 终态失败：创建任务时已扣的积分退回龙虾用户（速推侧失败退款时与本机余额对齐）
            if (
                token
                and upstream_tool == "get_result"
                and poll_task_id
                and isinstance(upstream_resp, dict)
                and not upstream_error
                and _sutui_get_result_is_terminal_failure(upstream_resp)
            ):
                refund_amt = _peek_task_billed_credits(poll_task_id)
                if refund_amt > 0:
                    ok_refund = await _post_task_failure_refund_with_retries(
                        token=token,
                        capability_id=capability_id,
                        poll_task_id=poll_task_id,
                        refund_amt=refund_amt,
                        request=request,
                        sutui_pool=sutui_pool_for_billing if upstream_name == "sutui" else None,
                        sutui_token_ref=sutui_token_ref_for_billing if upstream_name == "sutui" else None,
                    )
                    if ok_refund:
                        _pop_task_billed_credits(poll_task_id)
                        logger.info(
                            "[MCP] 任务终态失败退款 task_id=%s credits=%s（与速推创建任务扣费对应）",
                            poll_task_id,
                            refund_amt,
                        )
                    else:
                        logger.error(
                            "[MCP] 任务失败退款多次重试仍失败，已保留 task_id=%s 扣费缓存 credits=%s，避免未到账即丢弃记录",
                            poll_task_id,
                            refund_amt,
                        )
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
                # 任务完成，清理临时文件
                if poll_task_id:
                    try:
                        from backend.app.api.assets import cleanup_temp_files_for_task
                        cleanup_temp_files_for_task(poll_task_id)
                    except Exception as e:
                        logger.debug("[临时文件] 清理失败 task_id=%s error=%s", poll_task_id, e)

            actual_used = quantize_credits(0)
            if isinstance(upstream_resp, dict) and not upstream_error:
                if upstream_tool == "generate":
                    actual_used = extract_upstream_reported_credits(upstream_resp)
                elif upstream_tool == "get_result":
                    if _sutui_get_result_is_terminal_success(upstream_resp):
                        poll_credits = quantize_credits(extract_upstream_reported_credits(upstream_resp))
                        already = quantize_credits(create_billed_amount_peek)
                        extra = (
                            quantize_credits(poll_credits - already)
                            if poll_credits > already
                            else quantize_credits(0)
                        )
                        if extra > 0 and poll_task_id and not _consume_task_result_bill_once(poll_task_id):
                            extra = quantize_credits(0)
                        actual_used = extra
                else:
                    actual_used = extract_upstream_reported_credits(upstream_resp)

            settle_final = quantize_credits(0) if upstream_error else quantize_credits(actual_used)

            _UNDERSTAND_CAPS_SETTLE = ("image.understand", "video.understand")
            _settle_multiplier = 1.0
            if (
                upstream_name == "sutui"
                and upstream_tool == "generate"
                and capability_id not in _UNDERSTAND_CAPS_SETTLE
            ):
                try:
                    from backend.app.api.capabilities import _get_user_price_multiplier
                    _settle_multiplier = _get_user_price_multiplier()
                except Exception:
                    _settle_multiplier = 3.0

            if pre_deduct_amount > 0:
                pre_applied_flag = True
                bill_credits = pre_deduct_amount
                actual_user_price = quantize_credits(float(actual_used) * _settle_multiplier) if actual_used > 0 else quantize_credits(0)
                if upstream_error:
                    cf_out: Optional[int] = 0
                elif actual_used <= 0:
                    cf_out = None
                    logger.info(
                        "[MCP] 上游未返回实际扣费，保留预扣 %s 作为最终价格 capability_id=%s",
                        pre_deduct_amount, capability_id,
                    )
                elif actual_user_price == pre_deduct_amount:
                    cf_out = None
                else:
                    cf_out = float(actual_user_price)
                logger.info(
                    "[MCP] invoke_capability 计费 capability_id=%s pre_deduct=%s upstream_parsed=%s settle_final=%s multiplier=%.1f user_price=%s credits_final_out=%s",
                    capability_id, pre_deduct_amount, actual_used, settle_final, _settle_multiplier, actual_user_price, cf_out,
                )
                await _record_call(
                    token, record_capability_id, not bool(upstream_error), latency_ms, payload,
                    upstream_resp if isinstance(upstream_resp, dict) else {}, upstream_error or None,
                    credits_charged=(bill_credits if bill_credits > 0 else None),
                    pre_deduct_applied=pre_applied_flag,
                    credits_pre_deducted=pre_deduct_amount,
                    credits_final=cf_out,
                    request=request,
                    sutui_pool=sutui_pool_for_billing if upstream_name == "sutui" else None,
                    sutui_token_ref=sutui_token_ref_for_billing if upstream_name == "sutui" else None,
                )
            else:
                bill_credits = actual_used
                pre_applied_flag = False
                logger.info(
                    "[MCP] invoke_capability 计费 capability_id=%s pre_deduct=%s upstream_parsed=%s bill=%s pre_applied=%s",
                    capability_id, pre_deduct_amount, actual_used, bill_credits, pre_applied_flag,
                )
                await _record_call(
                    token, record_capability_id, not bool(upstream_error), latency_ms, payload,
                    upstream_resp if isinstance(upstream_resp, dict) else {}, upstream_error or None,
                    credits_charged=(bill_credits if bill_credits > 0 else None),
                    pre_deduct_applied=pre_applied_flag,
                    request=request,
                    sutui_pool=sutui_pool_for_billing if upstream_name == "sutui" else None,
                    sutui_token_ref=sutui_token_ref_for_billing if upstream_name == "sutui" else None,
                )
            if (
                upstream_tool == "generate"
                and not upstream_error
                and (settle_final > 0 or pre_deduct_amount > 0)
                and isinstance(upstream_resp, dict)
            ):
                created_tid = _extract_task_id_from_sutui_response(upstream_resp)
                if created_tid:
                    refund_on_fail = pre_deduct_amount if pre_deduct_amount > 0 else settle_final
                    _remember_task_billed_credits(created_tid, refund_on_fail)
                    logger.info(
                        "[MCP] 已记录创建任务扣费 task_id=%s credits=%s pre_deduct=%s settle=%s（失败时凭 task_id 退款）",
                        created_tid,
                        refund_on_fail,
                        pre_deduct_amount,
                        settle_final,
                    )
            logger.info("[MCP] invoke_capability 完成 capability_id=%s latency_ms=%s ok=%s", capability_id, latency_ms, not bool(upstream_error))
            data: Dict[str, Any] = {"capability_id": capability_id, "result": _redact_sensitive(upstream_resp)}
            if settle_final > 0:
                data["credits_used"] = settle_final

            # 自动入库：一次生成任务只入库一轮；该轮可含多个资源（多 URL）。异步 generate 不入库；get_result 仅终态且同一 task_id 只入库一次（轮询会多次终态成功）。
            # 云端 API 场景可设 MCP_AUTOSAVE_ASSETS=0，避免与「素材仅本机」冲突，由本机 lobster_online 保存。
            if not upstream_error and MCP_AUTOSAVE_ASSETS_ENABLED:
                should_autosave = False
                if upstream_tool == "get_result":
                    if _sutui_get_result_is_terminal_success(upstream_resp):
                        tid_for_save = poll_task_id or _extract_task_id_from_sutui_response(upstream_resp)
                        if not tid_for_save:
                            stable_urls = _prefer_stable_urls_for_autosave(
                                _extract_media_urls_for_auto_save(upstream_resp)
                            )
                            if stable_urls:
                                tid_for_save = "fp:" + hashlib.sha256(
                                    stable_urls[0].strip().lower().encode("utf-8")
                                ).hexdigest()[:32]
                        should_autosave = _consume_task_autosave_once(tid_for_save)
                        if not should_autosave:
                            logger.info(
                                "[MCP auto_save] skip duplicate autosave for task_id=%s",
                                (tid_for_save[:20] + "…") if tid_for_save and len(tid_for_save) > 20 else (tid_for_save or "(empty)"),
                            )
                elif upstream_tool == "generate":
                    created_async = _extract_task_id_from_sutui_response(upstream_resp)
                    should_autosave = not bool(created_async)
                else:
                    should_autosave = True
                if should_autosave:
                    saved = await _auto_save_generated_assets(upstream_resp, capability_id, payload, token, request=request)
                    if saved:
                        data["saved_assets"] = saved

            text = _json_dumps_mcp_payload(data)
            return [{"type": "text", "text": text}], bool(upstream_error)

        if name == "save_asset":
            url = str(args.get("url") or "").strip()
            if not url:
                return [{"type": "text", "text": "请提供素材 URL"}], True
            body = {
                "url": url,
                "media_type": args.get("media_type", "image"),
                "tags": args.get("tags", ""),
                "prompt": args.get("prompt", ""),
            }
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(f"{BASE_URL}/api/assets/save-url", json=body, headers=_backend_headers(token, request))
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
                r = await client.get(f"{BASE_URL}/api/assets", params=params_qs, headers=_backend_headers(token, request))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        # ── Meta Social（Instagram / Facebook）工具 ──

        if name == "list_meta_social_accounts":
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{BASE_URL}/api/meta-social/accounts", headers=_backend_headers(token, request))
            data = r.json() if r.content else []
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "publish_meta_social":
            body = {
                "account_id": args.get("account_id"),
                "platform": args.get("platform", "instagram"),
                "content_type": args.get("content_type", "photo"),
            }
            for k in ("asset_id", "image_url", "video_url", "caption", "message", "link", "title", "tags", "carousel_items"):
                v = args.get(k)
                if v is not None:
                    body[k] = v
            logger.info("[MCP] publish_meta_social body=%s", {k: v for k, v in body.items() if k != "caption"})
            async with httpx.AsyncClient(timeout=600.0) as client:
                r = await client.post(
                    f"{BASE_URL}/api/meta-social/publish",
                    headers=_backend_headers(token, request),
                    json=body,
                )
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "get_meta_social_data":
            params: Dict[str, Any] = {}
            if args.get("account_id"):
                params["account_id"] = args["account_id"]
            if args.get("platform"):
                params["platform"] = args["platform"]
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(
                    f"{BASE_URL}/api/meta-social/data",
                    headers=_backend_headers(token, request),
                    params=params,
                )
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "sync_meta_social_data":
            params_sync: Dict[str, Any] = {}
            if args.get("account_id"):
                params_sync["account_id"] = args["account_id"]
            logger.info("[MCP] sync_meta_social_data params=%s", params_sync)
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    f"{BASE_URL}/api/meta-social/sync",
                    headers=_backend_headers(token, request),
                    params=params_sync,
                )
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "get_social_report":
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(
                    f"{BASE_URL}/api/meta-social/data",
                    headers=_backend_headers(token, request),
                )
            if r.status_code >= 400:
                data = r.json() if r.content else {}
                text = json.dumps(data, ensure_ascii=False, indent=2)
                return [{"type": "text", "text": text}], True
            all_data = r.json() if r.content else {}
            entries = all_data.get("data", [])
            if not entries:
                return [{"type": "text", "text": json.dumps({"hint": "暂无已连接的 IG/FB 账号数据。请先连接账号并调用 sync_meta_social_data 同步数据。"}, ensure_ascii=False)}], False

            report: Dict[str, Any] = {"platforms": {}, "summary": {}}
            total_posts = 0
            total_likes = 0
            total_comments = 0
            for entry in entries:
                acct = entry.get("account", {})
                plat = acct.get("platform", "unknown")
                label = acct.get("label") or acct.get("username") or acct.get("page_name") or ""
                posts = entry.get("posts", [])
                metrics = entry.get("account_metrics", {})

                plat_likes = sum(p.get("like_count", 0) or p.get("likes", 0) for p in posts)
                plat_comments = sum(p.get("comments_count", 0) or p.get("comments", 0) for p in posts)

                key = f"{plat}_{acct.get('id', '')}"
                report["platforms"][key] = {
                    "platform": plat,
                    "label": label,
                    "post_count": len(posts),
                    "total_likes": plat_likes,
                    "total_comments": plat_comments,
                    "account_metrics": metrics,
                    "top_posts": sorted(posts, key=lambda p: (p.get("like_count", 0) or p.get("likes", 0)), reverse=True)[:3],
                    "fetched_at": entry.get("fetched_at"),
                }
                total_posts += len(posts)
                total_likes += plat_likes
                total_comments += plat_comments

            report["summary"] = {
                "total_platforms": len(report["platforms"]),
                "total_posts": total_posts,
                "total_likes": total_likes,
                "total_comments": total_comments,
            }
            text = json.dumps(report, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], False

        if name == "list_publish_accounts":
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{BASE_URL}/api/accounts", headers=_backend_headers(token, request))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "open_account_browser":
            nickname = str(args.get("account_nickname") or "").strip()
            if not nickname:
                return [{"type": "text", "text": "请提供 account_nickname"}], True
            acct_id = await _find_account_id_by_nickname(nickname, token, request)
            if not acct_id:
                return [{"type": "text", "text": f"找不到昵称为「{nickname}」的账号，请先在「发布管理」中添加"}], True
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(f"{BASE_URL}/api/accounts/{acct_id}/open-browser", headers=_backend_headers(token, request))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "check_account_login":
            nickname = str(args.get("account_nickname") or "").strip()
            if not nickname:
                return [{"type": "text", "text": "请提供 account_nickname"}], True
            acct_id = await _find_account_id_by_nickname(nickname, token, request)
            if not acct_id:
                return [{"type": "text", "text": f"找不到昵称为「{nickname}」的账号"}], True
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{BASE_URL}/api/accounts/{acct_id}/login-status", headers=_backend_headers(token, request))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "publish_content":
            asset_id = str(args.get("asset_id") or "").strip()
            account_nickname = str(args.get("account_nickname") or "").strip()
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
                r = await client.post(f"{BASE_URL}/api/publish", json=body, headers=_backend_headers(token, request))
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
        token = _get_token_from_request(request)
        is_admin = await _fetch_is_skill_store_admin(token)
        allowed = await _fetch_user_allowed_capability_ids(token)
        tools = _tool_definitions(catalog, is_skill_store_admin=is_admin, allowed_capability_ids=allowed)
        logger.info("[MCP] tools/list -> %s tools (allowed=%s)", len(tools), "all" if allowed is None else len(allowed))
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        cap_id = str(arguments.get("capability_id") or "").strip() if name == "invoke_capability" else ""
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
