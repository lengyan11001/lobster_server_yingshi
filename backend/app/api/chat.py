"""Chat endpoint — direct LLM API with MCP tool-calling loop.

Primary flow:
  POST /chat → resolve model + API key → fetch MCP tools from local MCP server
  → call LLM with function definitions + messages → process tool_calls → loop
  → return final reply

POST /chat/stream: same but streams SSE progress (tool_start/tool_end) so the UI can show "thinking" steps.
Falls back to OpenClaw Gateway when no direct API config is available.
"""
from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import SessionLocal, get_db
from .auth import access_token_claims, create_access_token, get_current_user, oauth2_scheme
# 算力账号已去掉，速推统一走服务器配置 Token（MCP 侧负载均衡）
# from .consumption_accounts import get_effective_sutui_token
from ..models import CapabilityCallLog, ChatTurnLog, ToolCallLog, User

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_HISTORY = 20
MAX_TOOL_ROUNDS = 8
# 单条历史消息最大字符数，避免长回复再次送入模型导致重复/延续上一条
MAX_HISTORY_MESSAGE_CHARS = 1200
MCP_URL = "http://127.0.0.1:8001/mcp"

_URL_RE = re.compile(r'https?://[^\s"\'<>\)\]]+', re.IGNORECASE)
_pending_tool_logs: contextvars.ContextVar[List[Dict]] = contextvars.ContextVar("_pending_tool_logs", default=[])
# 供 task.get_result 轮询遇 504 时自动重提 video.generate（同请求内最近一次成功提交的参数）
_last_video_generate_invoke: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "_last_video_generate_invoke", default=None
)
# 上游 fal/网关以 JSON 返回 Unexpected status code: 504 时，自动重新提交视频任务的最多次数
_UPSTREAM_504_VIDEO_RESUBMIT_MAX = 3

_POLL_MAX_WAIT_IMAGE = 30 * 60   # 图片生成轮询上限 30 分钟
_POLL_MAX_WAIT_VIDEO = 60 * 60   # 视频生成轮询上限 60 分钟（Seedance 等慢模型需要更久）
_POLL_MAX_WAIT_GENERIC = 60 * 60 # task.get_result 通用轮询上限 60 分钟

_PROVIDERS: Dict[str, Dict[str, str]] = {
    "deepseek":  {"base_url": "https://api.deepseek.com",  "env": "DEEPSEEK_API_KEY"},
    "openai":    {"base_url": "https://api.openai.com",    "env": "OPENAI_API_KEY"},
    "anthropic": {"base_url": "https://api.anthropic.com", "env": "ANTHROPIC_API_KEY"},
    "google":    {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "env": "GEMINI_API_KEY"},
}

_NO_TOOL_SUPPORT = {"deepseek-reasoner", "o3-mini"}


# ── Pydantic models ───────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str = Field(..., description="user | assistant | system")
    content: str = Field(..., description="消息内容")


class ChatRequest(BaseModel):
    message: str = Field(..., description="当前用户输入")
    history: Optional[List[ChatMessage]] = Field(default_factory=list)
    session_id: Optional[str] = None
    context_id: Optional[str] = None
    model: Optional[str] = None
    attachment_asset_ids: Optional[List[str]] = Field(default_factory=list, description="本条消息附带的素材 ID，将生成可访问 URL 供速推等使用")
    attachment_image_urls: Optional[List[str]] = Field(default_factory=list, description="图生视频时直接传图片 URL（如上一轮生成的 saved_assets[].url），与 attachment_asset_ids 合并后注入 video.generate")


class ChatResponse(BaseModel):
    reply: str


# ── API key / provider resolution ─────────────────────────────────

def _all_api_keys() -> Dict[str, str]:
    """Merge keys from openclaw.json literal values and openclaw/.env."""
    keys: Dict[str, str] = {}
    try:
        p = _BASE_DIR / "openclaw" / "openclaw.json"
        if p.exists():
            text = p.read_text(encoding="utf-8")
            for pid, pd in json.loads(text).get("models", {}).get("providers", {}).items():
                if isinstance(pd, dict):
                    k = (pd.get("apiKey") or "").strip()
                    if k and not k.startswith("${"):
                        env_name = _PROVIDERS.get(pid, {}).get("env", "")
                        if env_name:
                            keys[env_name] = k
    except Exception:
        pass
    try:
        ep = _BASE_DIR / "openclaw" / ".env"
        if ep.exists():
            for line in ep.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    if v.strip():
                        keys[k.strip()] = v.strip()
    except Exception:
        pass
    return keys


def _resolve_config(model: str) -> Optional[Dict[str, Any]]:
    """Return {base_url, api_key, model_name, provider} or None."""
    if "/" not in model:
        return None
    provider, model_name = model.split("/", 1)
    pcfg = _PROVIDERS.get(provider)
    if not pcfg:
        return None
    api_key = _all_api_keys().get(pcfg["env"], "")
    if not api_key:
        return None
    return {
        "base_url": pcfg["base_url"],
        "api_key": api_key,
        "model_name": model_name,
        "provider": provider,
    }


def _pick_default_model() -> str:
    """Return the first model that has a configured API key."""
    try:
        p = _BASE_DIR / "openclaw" / "openclaw.json"
        if p.exists():
            primary = json.loads(p.read_text(encoding="utf-8")).get(
                "agents", {}
            ).get("defaults", {}).get("model", {}).get("primary", "")
            if primary and _resolve_config(primary):
                return primary
    except Exception:
        pass
    for first in ["deepseek/deepseek-chat", "openai/gpt-4o",
                   "anthropic/claude-sonnet-4-5", "google/gemini-2.5-pro"]:
        if _resolve_config(first):
            return first
    raise HTTPException(
        400,
        detail="未配置任何 LLM API Key，请到「系统配置」页面添加至少一个模型的 API Key（如 DeepSeek、OpenAI 等）",
    )


# ── MCP tool helpers ──────────────────────────────────────────────

async def _fetch_mcp_tools(raw_token: Optional[str] = None) -> List[Dict]:
    """Fetch available tools from the local MCP server (port 8001)。传入用户 JWT 以便 MCP 过滤调试中技能。"""
    try:
        headers = {}
        t = (raw_token or "").strip()
        if t:
            headers["Authorization"] = f"Bearer {t}" if not t.lower().startswith("bearer ") else t
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                MCP_URL,
                json={"jsonrpc": "2.0", "id": "lt", "method": "tools/list", "params": {}},
                headers=headers,
            )
        tools = r.json().get("result", {}).get("tools", [])
        logger.info("[对话] MCP tools/list 成功 tools_count=%s", len(tools))
        return tools
    except Exception as e:
        logger.warning("[对话] MCP tools/list 失败: %s", e)
        return []


async def get_customer_service_reply(
    user_message: str,
    company_info: str = "",
    product_intro: str = "",
    common_phrases: str = "",
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """客服专用：仅根据提供的公司信息、产品介绍、常用话术回复；匹配不到则只做简短闲聊，严禁编造。"""
    if not (user_message or "").strip():
        return "收到。"
    materials = []
    if (company_info or "").strip():
        materials.append("【公司信息】\n" + company_info.strip())
    if (product_intro or "").strip():
        materials.append("【产品介绍】\n" + product_intro.strip())
    if (common_phrases or "").strip():
        materials.append("【常用话术】\n" + common_phrases.strip())
    materials_text = "\n\n".join(materials) if materials else "（暂无资料）"
    sys = (
        "你是企业微信客服助手。你必须严格遵守以下规则：\n"
        "1. 仅根据下面「公司信息」「产品介绍」「常用话术」回答与公司、产品相关的问题。\n"
        "2. 若用户问题无法从上述资料中匹配到任何内容，只可做简短、友好的闲聊（如问候、感谢、请稍候联系人工），严禁编造公司名、产品名、价格、规格等任何未在资料中出现的信息。\n"
        "3. 回复简短、使用中文。\n\n"
        + materials_text
    )
    messages: List[Dict[str, str]] = [{"role": "system", "content": sys}]
    if history:
        for h in history[-10:]:
            if isinstance(h, dict) and h.get("role") and h.get("content"):
                messages.append({"role": h["role"], "content": str(h["content"])[:800]})
    messages.append({"role": "user", "content": (user_message or "").strip()})
    model = ""
    try:
        model = _pick_default_model()
    except HTTPException:
        model = "openclaw"
    cfg = _resolve_config(model) if model else None
    if cfg:
        try:
            reply = await _chat_openai(messages, cfg, [], "", sutui_token=None)
            return (reply or "").strip() or "收到。"
        except HTTPException:
            return "服务暂时不可用，请稍后再试。"
        except Exception as e:
            logger.exception("[客服回复] chat 异常: %s", e)
            return "处理时遇到问题，请稍后再试。"
    oc_reply = await _try_openclaw(messages, model or "openclaw", "")
    if oc_reply:
        return (oc_reply or "").strip() or "收到。"
    return "抱歉，当前未配置对话模型，无法回复。"


async def _exec_tool_with_balance(
    name: str,
    args: Dict,
    token: str,
    sutui_token: Optional[str],
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]],
    db: Optional[Session],
    user_id: Optional[int],
) -> str:
    """调用工具：invoke_capability 时先校验余额 >0，执行后按返回积分同步扣减。"""
    cap = (args.get("capability_id") or "").strip() if name == "invoke_capability" else None
    if name == "invoke_capability" and db is not None and user_id is not None:
        user = db.query(User).filter(User.id == user_id).first()
        from ..services.credits_amount import user_balance_decimal

        if user and user_balance_decimal(user) <= 0:
            return "积分不足：当前余额为 0，无法使用速推能力。请先充值。"
    res = await _exec_tool(name, args, token, sutui_token, progress_cb)
    # 积分扣减统一由 MCP → POST /capabilities/record-call 完成（含速推返回动态 credits_used）
    return res


async def _exec_tool(
    name: str,
    args: Dict,
    token: str = "",
    sutui_token: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
) -> str:
    """Execute a tool on the local MCP server and return the text result. progress_cb: 可选，用于流式推送进度（tool_start/tool_end）。"""
    capability_id = (args.get("capability_id") or "").strip() if name == "invoke_capability" else None
    phase = None
    if capability_id == "video.generate":
        phase = "video_submit"
    elif capability_id == "image.generate":
        phase = "image_submit"
    elif capability_id == "task.get_result":
        phase = "task_polling"
    ev_start = {"type": "tool_start", "name": name, "args": list(args.keys())}
    if capability_id is not None:
        ev_start["capability_id"] = capability_id
    if phase:
        ev_start["phase"] = phase
    if progress_cb:
        try:
            await progress_cb(ev_start)
        except Exception:
            pass
    if capability_id in ("image.generate", "video.generate", "task.get_result"):
        pl = args.get("payload") or {}
        if capability_id == "task.get_result":
            logger.info(
                "[素材] MCP 请求 capability_id=%s task_id=%s",
                capability_id, (pl.get("task_id") or "").strip() or "(空)",
            )
        else:
            logger.info(
                "[素材] MCP 请求 capability_id=%s payload: model=%s prompt_len=%d image_url=%s",
                capability_id,
                (pl.get("model") or "").strip() or "(无)",
                len((pl.get("prompt") or "").strip()),
                "有" if (pl.get("image_url") or pl.get("media_files")) else "无",
            )
    t0 = time.perf_counter()
    success = True
    result_text = ""
    timeout = 120.0
    if name == "invoke_capability" and (args.get("capability_id") or "").strip() == "task.get_result":
        timeout = 65 * 60.0  # 单次 get_result 调用超时，须大于 _POLL_MAX_WAIT_VIDEO

    def _friendly_tool_error(err: Exception) -> str:
        raw = str(err or "")
        low = raw.lower()
        if (
            "getaddrinfo failed" in low
            or "name or service not known" in low
            or "nodename nor servname provided" in low
            or "temporary failure in name resolution" in low
        ):
            return (
                "网络解析失败（DNS）：无法解析上游接口域名。"
                "请检查网络/代理/DNS 配置，确认可访问速推与模型 API 域名后重试。"
            )
        if "all connection attempts failed" in low or "connection refused" in low:
            return "网络连接失败：无法连接到目标服务，请检查网络、端口或服务是否启动。"
        if "timed out" in low or "timeout" in low:
            return "请求超时：上游响应过慢，请稍后重试。"
        return f"工具调用失败: {raw}"

    try:
        hdrs: Dict[str, str] = {"Content-Type": "application/json"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        if sutui_token:
            hdrs["X-Sutui-Token"] = sutui_token
        if capability_id == "video.generate":
            pl = args.get("payload") or {}
            img = (pl.get("image_url") or "")
            mf = pl.get("media_files") or []
            logger.info(
                "[CHAT] 发 MCP video.generate 完整 payload（将原样转速推）: image_url=%s media_files=%s",
                (img[:100] + "…") if len(img) > 100 else (img or "(无)"),
                mf,
            )
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(MCP_URL, json={
                "jsonrpc": "2.0", "id": "ct",
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }, headers=hdrs)
        try:
            body = r.json()
        except Exception:
            result_text = (r.text or "")[:2000] or f"MCP 响应非 JSON（HTTP {r.status_code}）"
            success = False
        else:
            if r.status_code >= 400:
                err = body.get("error") if isinstance(body, dict) else None
                if isinstance(err, dict):
                    result_text = str(err.get("message") or err)[:2000]
                else:
                    result_text = str(body)[:2000]
                success = False
            elif isinstance(body, dict) and body.get("error"):
                err = body.get("error")
                result_text = (
                    str(err.get("message")) if isinstance(err, dict) else str(err)
                )[:2000]
                success = False
            else:
                res = body.get("result") if isinstance(body, dict) else None
                if not isinstance(res, dict):
                    res = {}
                content = res.get("content", [])
                if isinstance(content, list) and content:
                    texts = [
                        x.get("text", "")
                        for x in content
                        if isinstance(x, dict) and x.get("type") == "text"
                    ]
                    result_text = "\n".join(t for t in texts if t) or json.dumps(content, ensure_ascii=False)
                else:
                    result_text = json.dumps(res, ensure_ascii=False)
                if res.get("isError"):
                    success = False
    except Exception as e:
        result_text = _friendly_tool_error(e)
        success = False
        logger.warning("[对话] 工具执行异常 name=%s capability_id=%s: %s", name, capability_id, e)

    ms = round((time.perf_counter() - t0) * 1000)
    logger.info("[对话] 工具执行 name=%s capability_id=%s latency_ms=%s success=%s", name, capability_id or "-", ms, success)
    if capability_id in ("image.generate", "video.generate", "task.get_result"):
        in_prog = _is_task_result_in_progress(result_text)
        task_id = _extract_task_id_from_result(result_text) if result_text else ""
        logger.info(
            "[素材] MCP 返回 capability_id=%s result_len=%d in_progress=%s task_id=%s preview=%s",
            capability_id, len(result_text or ""), in_prog, task_id or "(无)", (result_text or "")[:120],
        )
    urls = _URL_RE.findall(result_text)
    try:
        logs = _pending_tool_logs.get()
    except LookupError:
        logs = []
        _pending_tool_logs.set(logs)
    logs.append({
        "tool_name": name,
        "arguments": args,
        "result_text": result_text[:10000],
        "result_urls": ",".join(urls[:20]) if urls else None,
        "success": success,
        "latency_ms": ms,
    })
    ev_end = {
        "type": "tool_end",
        "name": name,
        "preview": (result_text or "")[:200],
        "success": success,
    }
    if capability_id is not None:
        ev_end["capability_id"] = capability_id
    if phase:
        ev_end["phase"] = phase
    if phase == "task_polling":
        ev_end["in_progress"] = _is_task_result_in_progress(result_text)
        if not ev_end.get("in_progress"):
            ev_end["media_type"] = _extract_media_type_from_task_result(result_text)
            # 任务完成，清理临时文件
            task_id = _extract_task_id_from_result(result_text)
            if task_id:
                try:
                    from backend.app.api.assets import cleanup_temp_files_for_task
                    cleanup_temp_files_for_task(task_id)
                except Exception as e:
                    logger.debug("[临时文件] 清理失败 task_id=%s error=%s", task_id, e)
        logger.info(
            "[进度] task.get_result 单次返回 in_progress=%s status=%s",
            ev_end.get("in_progress"),
            _extract_status_for_log(result_text),
        )
    if progress_cb:
        try:
            await progress_cb(ev_end)
        except Exception:
            pass
    if (
        capability_id == "video.generate"
        and success
        and result_text
        and _is_task_result_in_progress(result_text)
        and _extract_task_id_from_result(result_text)
    ):
        try:
            _last_video_generate_invoke.set(copy.deepcopy(args))
        except Exception:
            logger.debug("[素材] 记录 video.generate 参数供504重试用失败", exc_info=True)
    return result_text


# ── LLM API calls with tool-calling loop ──────────────────────────

_PROVIDER_NAMES = {
    "deepseek": "DeepSeek", "openai": "OpenAI",
    "anthropic": "Anthropic", "google": "Google Gemini",
}

def _raise_api_err(resp: httpx.Response, model: str = ""):
    detail = resp.text[:500]
    try:
        e = resp.json().get("error", {})
        detail = (e.get("message") if isinstance(e, dict) else str(e)) or detail
    except Exception:
        pass
    if resp.status_code in (401, 403):
        provider = model.split("/", 1)[0] if "/" in model else ""
        name = _PROVIDER_NAMES.get(provider, provider or "LLM")
        raise HTTPException(
            502,
            detail=f"{name} API Key 无效或未配置，请到「系统配置」页面设置正确的 API Key。",
        )
    raise HTTPException(502, detail=f"LLM API 错误 ({resp.status_code}): {detail}")


_DSML_FC_RE = re.compile(
    r'<[\uff5c|]DSML[\uff5c|]function_calls>(.*?)</[\uff5c|]DSML[\uff5c|]function_calls>',
    re.DOTALL,
)
_DSML_INVOKE_RE = re.compile(
    r'<[\uff5c|]DSML[\uff5c|]invoke\s+name="([^"]+)">(.*?)</[\uff5c|]DSML[\uff5c|]invoke>',
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    r'<[\uff5c|]DSML[\uff5c|]parameter\s+name="([^"]+)"\s+string="(true|false)">(.*?)</[\uff5c|]DSML[\uff5c|]parameter>',
    re.DOTALL,
)


def _parse_text_tool_calls(content: str) -> List[Dict[str, Any]]:
    """Parse DeepSeek DSML or similar text-embedded tool calls."""
    calls: List[Dict[str, Any]] = []
    for fc_match in _DSML_FC_RE.finditer(content):
        block = fc_match.group(1)
        for inv in _DSML_INVOKE_RE.finditer(block):
            name = inv.group(1)
            body = inv.group(2)
            args: Dict[str, Any] = {}
            for pm in _DSML_PARAM_RE.finditer(body):
                pname, is_str, pvalue = pm.group(1), pm.group(2), pm.group(3).strip()
                if is_str == "false":
                    try:
                        pvalue = json.loads(pvalue)
                    except Exception:
                        pass
                args[pname] = pvalue
            calls.append({"name": name, "arguments": args})
    return calls


def _strip_dsml(content: str) -> str:
    """Remove DSML markup from text content, return readable portion."""
    cleaned = _DSML_FC_RE.sub("", content).strip()
    cleaned = re.sub(r'<[\uff5c|]DSML[\uff5c|][^>]*>', '', cleaned).strip()
    return cleaned


def _reply_for_user(reply: str) -> str:
    """Strip DSML from reply so user never sees raw function_calls; use friendly text if nothing left."""
    out = _strip_dsml(reply or "").strip()
    if not out:
        return "正在处理…"
    return out


# 速推 task.get_result 状态：先判进行中再判终态，避免「未完成」等误判
_TASK_TERMINAL_STATUSES = (
    "success", "completed", "done", "succeeded", "finished",
    "failed", "error", "cancelled", "canceled", "timeout", "expired",
    "已完成", "生成成功", "成功", "完成", "失败", "错误", "取消", "超时",
)
_TASK_IN_PROGRESS_STATUSES = (
    "pending", "queued", "submitted", "processing", "generating", "running",
    "处理中", "生成中", "排队中", "运行中", "上传中", "等待中",
)


def _extract_media_type_from_task_result(result_text: str) -> str:
    """从 task.get_result 返回的 JSON 中解析 saved_assets[0].media_type。支持 MCP 嵌套 d.result.result.content[0].text."""
    if not result_text or not result_text.strip():
        return "video"
    raw = (result_text or "").strip()
    try:
        d = json.loads(raw) if raw.startswith("{") else {}
        saved = d.get("saved_assets") or (d.get("result") or {}).get("saved_assets")
        if isinstance(saved, list) and saved:
            mt = (saved[0].get("media_type") or "").strip().lower()
            if mt in ("image", "video"):
                return mt
        upstream = d.get("result")
        if isinstance(upstream, dict):
            inner_result = upstream.get("result")
            if isinstance(inner_result, dict):
                content = inner_result.get("content") or []
                if content and isinstance(content[0], dict):
                    t = (content[0].get("text") or "").strip()
                    if t.startswith("{"):
                        obj = json.loads(t)
                        saved = obj.get("saved_assets") or []
                        if isinstance(saved, list) and saved:
                            mt = (saved[0].get("media_type") or "").strip().lower()
                            if mt in ("image", "video"):
                                return mt
    except Exception:
        pass
    return "video"


def _normalize_saved_asset_item(x: Dict[str, Any]) -> Dict[str, Any]:
    """归一化单条 saved_asset：asset_id / url / media_type。"""
    url = (
        x.get("url") or x.get("file_path") or x.get("path") or x.get("file_url") or x.get("image_url") or ""
    ).strip()
    return {
        "asset_id": (x.get("asset_id") or "").strip(),
        "url": url,
        "media_type": (x.get("media_type") or "").strip().lower() or "image",
    }


def _log_task_result_structure(d: Dict[str, Any], raw: str) -> None:
    """打印 task.get_result 解析到的数据结构，便于下次直接看到上游返回格式。"""
    try:
        top_keys = list(d.keys()) if isinstance(d, dict) else []
        res = d.get("result")
        res_keys = list(res.keys()) if isinstance(res, dict) else None
        inner = res.get("result") if isinstance(res, dict) else None
        inner_keys = list(inner.keys()) if isinstance(inner, dict) else None
        content = (inner.get("content") if isinstance(inner, dict) else None) or (res.get("content") if isinstance(res, dict) else None) or []
        content_len = len(content) if isinstance(content, list) else 0
        first_text = ""
        if isinstance(content, list) and content and isinstance(content[0], dict):
            first_text = (content[0].get("text") or "")[:500]
        logger.info(
            "[素材] task.get_result 数据结构 keys=%s result_keys=%s result.result_keys=%s content_len=%s first_text_preview=%s",
            top_keys,
            res_keys,
            inner_keys,
            content_len,
            first_text[:300] + "..." if len(first_text) > 300 else first_text or "(无)",
        )
    except Exception:
        pass


def _extract_saved_assets_from_task_result(result_text: str) -> List[Dict[str, Any]]:
    """从 task.get_result 返回的 JSON 中解析 saved_assets 列表，供前端展示。支持 MCP 包装、上游 result.result.content[].text 及多层级 result。"""
    if not result_text or not result_text.strip():
        return []
    raw = (result_text or "").strip()

    def to_list(obj: Any) -> List[Dict[str, Any]]:
        if not isinstance(obj, list) or not obj:
            return []
        out = []
        for x in obj:
            if not isinstance(x, dict):
                continue
            item = _normalize_saved_asset_item(x)
            if item.get("asset_id") or item.get("url"):
                out.append(item)
        return out

    def from_output_images(obj: Any) -> List[Dict[str, Any]]:
        """上游 fal/速推 返回 output.images[].url，无 saved_assets 时从此处取。"""
        if not isinstance(obj, dict):
            return []
        output = obj.get("output")
        if not isinstance(output, dict):
            return []
        images = output.get("images")
        if not isinstance(images, list) or not images:
            return []
        return to_list(images)

    try:
        d = json.loads(raw) if raw.startswith("{") else {}
        if not d:
            return []

        # 打印结构便于下次对照解析
        _log_task_result_structure(d, raw)

        # 1) 顶层或 result 下直接有 saved_assets（MCP 或上游）
        saved = d.get("saved_assets") or (d.get("result") or {}).get("saved_assets")
        if isinstance(saved, list) and saved:
            out = to_list(saved)
            if out:
                return out

        # 2) 从 result.result.content[].text 等嵌套 JSON 里找 saved_assets
        upstream = d.get("result")
        if isinstance(upstream, dict):
            # 2a) result.result.content[0].text
            inner = upstream.get("result")
            if isinstance(inner, dict):
                for content in (inner.get("content") or [])[:5]:
                    if not isinstance(content, dict):
                        continue
                    t = (content.get("text") or "").strip()
                    if not t.startswith("{"):
                        continue
                    try:
                        obj = json.loads(t)
                        saved = obj.get("saved_assets") or (obj.get("result") or {}).get("saved_assets")
                        if isinstance(saved, list) and saved:
                            out = to_list(saved)
                            if out:
                                return out
                        out = from_output_images(obj)
                        if out:
                            return out
                    except Exception:
                        pass
            # 2b) result.content[0].text（上游直接 content）
            for content in (upstream.get("content") or [])[:5]:
                if not isinstance(content, dict):
                    continue
                t = (content.get("text") or "").strip()
                if not t.startswith("{"):
                    continue
                try:
                    obj = json.loads(t)
                    saved = obj.get("saved_assets") or (obj.get("result") or {}).get("saved_assets")
                    if isinstance(saved, list) and saved:
                        out = to_list(saved)
                        if out:
                            return out
                    out = from_output_images(obj)
                    if out:
                        return out
                except Exception:
                    pass
    except Exception:
        pass
    return []


def _extract_status_for_log(result_text: str) -> str:
    """从 task.get_result 返回文本中解析 status，仅用于日志。路径见 docs/图生视频_MCP调用流程与参数.md"""
    if not result_text or not result_text.strip():
        return "?"
    raw = (result_text or "").strip()

    def _get_status(obj: Any) -> str:
        if not isinstance(obj, dict):
            return ""
        s = (obj.get("status") or "").strip()
        if s:
            return s
        res = obj.get("result")
        if isinstance(res, dict):
            content = res.get("content") or []
            for c in content[:3]:
                if isinstance(c, dict):
                    t = (c.get("text") or "").strip()
                    if t.startswith("{"):
                        try:
                            inner = json.loads(t)
                            s = (inner.get("status") or _get_status(inner.get("result") or {}) or "").strip()
                            if s:
                                return s
                        except Exception:
                            pass
        return ""

    try:
        d = json.loads(raw) if raw.startswith("{") else {}
        if not d:
            for part in (raw.split("```") or [raw]):
                part = part.strip()
                if part.startswith("{") and "status" in part:
                    try:
                        d = json.loads(part)
                        break
                    except Exception:
                        pass
        if not d:
            return "?"
        upstream = d.get("result")
        if isinstance(upstream, dict):
            inner_result = upstream.get("result")
            if isinstance(inner_result, dict):
                content = inner_result.get("content") or []
                if content and isinstance(content[0], dict):
                    t = (content[0].get("text") or "").strip()
                    if t.startswith("{"):
                        try:
                            obj = json.loads(t)
                            s = (obj.get("status") or "").strip()
                            if s:
                                return s
                        except Exception:
                            pass
        s = _get_status(d) or _get_status(upstream or {})
        if s:
            return s
        m = re.search(r'"status"\s*:\s*"([^"]*)"', raw)
        if m and m.group(1).strip():
            return m.group(1).strip()
        return "?"
    except Exception:
        pass
    m = re.search(r'"status"\s*:\s*"([^"]*)"', (result_text or ""))
    if m and m.group(1).strip():
        return m.group(1).strip()
    return "?"


def _is_task_result_in_progress(result_text: str) -> bool:
    """True if task.get_result 表示仍在进行中（需继续 15s 轮询）。先判进行中再判终态，避免「未完成」等误判为终态."""
    if not result_text or not result_text.strip():
        return True  # 无内容时继续轮询，避免误报「已生成」
    raw = (result_text or "").strip()
    status_val = _extract_status_for_log(result_text)
    if status_val and status_val != "?":
        s = status_val.strip().lower()
        for term in _TASK_IN_PROGRESS_STATUSES:
            if s == term.lower():
                return True
        for term in _TASK_TERMINAL_STATUSES:
            if s == term.lower():
                return False
        return True
    raw_lower = raw.lower()
    if '"status":"completed"' in raw_lower or '"status":"success"' in raw_lower or '"status":"failed"' in raw_lower:
        return False
    if "未完成" in raw or "未成功" in raw:
        return True
    for s in _TASK_IN_PROGRESS_STATUSES:
        if s in raw_lower or f'"status":"{s}"' in raw_lower:
            return True
    for s in _TASK_TERMINAL_STATUSES:
        if s not in raw_lower:
            continue
        if s in ("完成", "成功") and ("未完成" in raw or "未成功" in raw):
            continue
        return False
    return True


def _task_result_hint(result_text: str) -> str:
    """从 task.get_result 返回文本中提取一句简短状态，供前端展示「查询结果」。status 与 _extract_status_for_log 同路径."""
    if not result_text or not result_text.strip():
        return ""
    status = _extract_status_for_log(result_text)
    if status and status != "?":
        if _is_task_result_in_progress(result_text):
            return f"当前状态: {status}"
        return f"结果: {status}"
    if _is_task_result_in_progress(result_text):
        return "当前状态: 仍生成中"
    return "结果: 已完成"


def _extract_task_id_from_result(result_text: str) -> str:
    """从 video.generate 或 task.get_result 的返回文本中解析 task_id。路径与 status 一致：d.result.result.content[0].text."""
    if not result_text or not result_text.strip():
        return ""
    raw = (result_text or "").strip()
    try:
        d = json.loads(raw) if raw.startswith("{") else {}
        if not d:
            return ""
        tid = (d.get("task_id") or "").strip()
        if tid:
            return tid
        upstream = d.get("result")
        if isinstance(upstream, dict):
            tid = (upstream.get("task_id") or "").strip()
            if tid:
                return tid
            inner_result = upstream.get("result")
            if isinstance(inner_result, dict):
                content = inner_result.get("content") or []
                if content and isinstance(content[0], dict):
                    t = (content[0].get("text") or "").strip()
                    if t.startswith("{"):
                        obj = json.loads(t)
                        tid = (obj.get("task_id") or "").strip()
                        if tid:
                            return tid
        return ""
    except Exception:
        return ""


def _is_sutui_task_upstream_504_failure(result_text: str) -> bool:
    """速推任务终态里 output 常见 raw_error: Unexpected status code: 504（非 HTTP 层 504）。"""
    if not result_text or not result_text.strip():
        return False
    low = result_text.lower()
    return "unexpected status code: 504" in low


def _task_result_looks_like_video_task(result_text: str) -> bool:
    """query 回包 JSON 中含视频模型 id 时才允许用 ContextVar 做 504 重提，避免图片任务误用视频参数。"""
    if not result_text:
        return False
    low = result_text.lower()
    return any(
        k in low
        for k in (
            "text-to-video",
            "image-to-video",
            "sora-2",
            "seedance",
            "super-seed",
            "kling-video",
            "wan/v",
            "veo3",
            "hailuo",
            "vidu",
            "grok-imagine-video",
            "jimeng",
        )
    )


async def _poll_task_until_terminal_then_retry_video_on_504(
    *,
    initial_res: str,
    get_result_args_base: Dict[str, Any],
    token: str,
    sutui_token: Optional[str],
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]],
    db: Optional[Session],
    user_id: Optional[int],
    tool_fn_name: str,
    poll_interval: int,
    max_wait_sec: int,
    enable_504_video_retry: bool,
    video_resubmit_args: Optional[Dict[str, Any]],
    log_label: str,
) -> str:
    """
    与 image/video 生成一致：先按间隔轮询 task.get_result 至终态。
    若启用 enable_504_video_retry 且终态为上游 504，则用 video_resubmit_args 或 ContextVar 中最近一次
    video.generate 参数重新提交，最多 _UPSTREAM_504_VIDEO_RESUBMIT_MAX 次。
    """
    res = initial_res
    retries_left = _UPSTREAM_504_VIDEO_RESUBMIT_MAX
    gr_args = dict(get_result_args_base)

    while True:
        pl = gr_args.get("payload") if isinstance(gr_args.get("payload"), dict) else {}
        task_id = (pl.get("task_id") or "").strip()

        if _is_task_result_in_progress(res):
            waited = 0
            logger.info(
                "[%s] 自动轮询开始 task_id=%s interval=%ds max_wait=%ds",
                log_label,
                task_id or "(无)",
                poll_interval,
                max_wait_sec,
            )
            while waited < max_wait_sec:
                await asyncio.sleep(poll_interval)
                waited += poll_interval
                res = await _exec_tool_with_balance(
                    tool_fn_name, gr_args, token, sutui_token, None, db, user_id,
                )
                in_prog = _is_task_result_in_progress(res)
                logger.info(
                    "[%s] 轮询 task.get_result waited=%ds task_id=%s in_progress=%s hint=%s",
                    log_label,
                    waited,
                    task_id or "(无)",
                    in_prog,
                    _task_result_hint(res),
                )
                if progress_cb:
                    try:
                        ev = {"type": "task_poll", "message": f"正在查询生成结果…（{waited}秒）"}
                        if task_id:
                            ev["task_id"] = task_id
                        ev["result_hint"] = _task_result_hint(res)
                        await progress_cb(ev)
                    except Exception:
                        pass
                if not in_prog:
                    saved = _extract_saved_assets_from_task_result(res)
                    logger.info(
                        "[%s] 轮询结束 task_id=%s saved_assets_count=%d asset_ids=%s",
                        log_label,
                        task_id or "(无)",
                        len(saved),
                        [x.get("asset_id") for x in saved],
                    )
                    break

        src = video_resubmit_args if video_resubmit_args is not None else _last_video_generate_invoke.get()
        uses_context_only = video_resubmit_args is None
        model_ok = (not uses_context_only) or _task_result_looks_like_video_task(res)
        can_retry = (
            enable_504_video_retry
            and retries_left > 0
            and _is_sutui_task_upstream_504_failure(res)
            and model_ok
            and isinstance(src, dict)
            and (src.get("capability_id") or "").strip() == "video.generate"
        )
        if not can_retry:
            return res

        retries_left -= 1
        # 终态失败时 MCP 在同一次 get_result 内会调 /capabilities/refund；稍候再提交新任务，
        # 让退款入账后再走 video.generate 的预扣，避免短时余额与账本不一致。
        await asyncio.sleep(2.0)
        logger.warning(
            "[%s] 上游返回 504，已等待 2s 后自动重新提交 video.generate（本轮后剩余重试 %d 次）",
            log_label,
            retries_left,
        )
        if progress_cb:
            try:
                await progress_cb({"type": "status", "message": "视频生成上游超时(504)，正在自动重试…"})
            except Exception:
                pass

        try:
            submit_args = copy.deepcopy(src)
        except Exception:
            submit_args = dict(src) if isinstance(src, dict) else {}

        res = await _exec_tool_with_balance(
            "invoke_capability",
            submit_args,
            token,
            sutui_token,
            progress_cb,
            db,
            user_id,
        )
        new_tid = _extract_task_id_from_result(res)
        if not new_tid:
            return res
        gr_args = {"capability_id": "task.get_result", "payload": {"task_id": new_tid}}
        if not _is_task_result_in_progress(res):
            if _is_sutui_task_upstream_504_failure(res) and retries_left > 0:
                continue
            return res


async def _chat_openai(
    msgs: List[Dict],
    cfg: Dict,
    mcp_tools: List[Dict],
    token: str,
    sutui_token: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    attachment_urls: Optional[List[str]] = None,
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> str:
    """OpenAI-compatible chat loop (DeepSeek, OpenAI, Google Gemini)."""
    base = cfg["base_url"].rstrip("/")
    if "googleapis.com" in base or base.endswith("/v1"):
        url = f"{base}/chat/completions"
    else:
        url = f"{base}/v1/chat/completions"

    hdrs = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    }
    model = cfg["model_name"]
    use_tools = model not in _NO_TOOL_SUPPORT and bool(mcp_tools)
    oai_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            },
        }
        for t in mcp_tools
    ] if use_tools else []

    _ACTION_KW = re.compile(
        r"(发布|生成|打开浏览器|帮你|开始|正在|马上|登录|查看素材|查看账号|"
        r"invoke_capability|publish_content|open_account_browser|list_assets)",
        re.IGNORECASE,
    )
    # 仅当用户当前消息包含操作意图时才强制要求调用工具，避免对「你好」等问候误触发
    _USER_ACTION_KW = re.compile(
        r"(帮我|给我|生成.*图|发.*抖音|发布到|打开浏览器|登录|查看素材|查看账号|"
        r"invoke_capability|publish_content|open_account_browser|list_assets|生成图片|发布内容)",
        re.IGNORECASE,
    )
    force_tool_retry_done = False

    cur = list(msgs)
    for rnd in range(MAX_TOOL_ROUNDS):
        body: Dict[str, Any] = {"model": model, "messages": cur, "stream": False}
        if oai_tools and rnd < MAX_TOOL_ROUNDS - 1:
            body["tools"] = oai_tools
            body["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=120.0) as c:
            resp = await c.post(url, json=body, headers=hdrs)
        if resp.status_code != 200:
            _raise_api_err(resp, model=f"{cfg.get('provider','')}/{cfg.get('model_name','')}")

        choice = (resp.json().get("choices") or [{}])[0]
        msg = choice.get("message", {})
        tcs = msg.get("tool_calls", [])

        if tcs:
            cur.append(msg)
            for tc in tcs:
                fn = tc.get("function", {})
                try:
                    a = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    a = {}
                _inject_video_media_urls(a, attachment_urls or [])
                logger.info("[CHAT] tool_call: %s(%s)", fn.get("name"), list(a.keys()))
                cap = (a.get("capability_id") or "").strip()
                if cap in ("image.generate", "video.generate", "task.get_result"):
                    logger.info("[素材] 模型请求工具 capability_id=%s", cap)
                res = await _exec_tool_with_balance(
                    fn.get("name", ""), a, token, sutui_token, progress_cb, db, user_id,
                )
                if (
                    fn.get("name") == "invoke_capability"
                    and cap == "image.generate"
                    and _is_task_result_in_progress(res)
                ):
                    task_id = _extract_task_id_from_result(res)
                    if task_id:
                        get_result_args = {"capability_id": "task.get_result", "payload": {"task_id": task_id}}
                        poll_interval = 15
                        max_wait_sec = _POLL_MAX_WAIT_IMAGE
                        res = await _poll_task_until_terminal_then_retry_video_on_504(
                            initial_res=res,
                            get_result_args_base=get_result_args,
                            token=token,
                            sutui_token=sutui_token,
                            progress_cb=progress_cb,
                            db=db,
                            user_id=user_id,
                            tool_fn_name=fn.get("name", "invoke_capability"),
                            poll_interval=poll_interval,
                            max_wait_sec=max_wait_sec,
                            enable_504_video_retry=False,
                            video_resubmit_args=None,
                            log_label="素材] image.generate",
                        )
                        if progress_cb:
                            try:
                                ev_end = {
                                    "type": "tool_end",
                                    "name": "invoke_capability",
                                    "preview": (res or "")[:200],
                                    "capability_id": "task.get_result",
                                    "phase": "task_polling",
                                    "in_progress": False,
                                }
                                ev_end["media_type"] = _extract_media_type_from_task_result(res)
                                ev_end["saved_assets"] = _extract_saved_assets_from_task_result(res)
                                logger.info("[素材] 推送 tool_end phase=task_polling(图片) saved_assets_count=%d", len(ev_end.get("saved_assets") or []))
                                await progress_cb(ev_end)
                                await progress_cb({"type": "status", "message": "正在生成回复…"})
                            except Exception:
                                pass
                elif (
                    fn.get("name") == "invoke_capability"
                    and cap == "video.generate"
                    and _is_task_result_in_progress(res)
                ):
                    task_id = _extract_task_id_from_result(res)
                    if task_id:
                        get_result_args = {"capability_id": "task.get_result", "payload": {"task_id": task_id}}
                        poll_interval = 15
                        max_wait_sec = _POLL_MAX_WAIT_VIDEO
                        video_args = copy.deepcopy(a)
                        res = await _poll_task_until_terminal_then_retry_video_on_504(
                            initial_res=res,
                            get_result_args_base=get_result_args,
                            token=token,
                            sutui_token=sutui_token,
                            progress_cb=progress_cb,
                            db=db,
                            user_id=user_id,
                            tool_fn_name=fn.get("name", "invoke_capability"),
                            poll_interval=poll_interval,
                            max_wait_sec=max_wait_sec,
                            enable_504_video_retry=True,
                            video_resubmit_args=video_args,
                            log_label="素材] video.generate",
                        )
                        if progress_cb:
                            try:
                                ev_end = {
                                    "type": "tool_end",
                                    "name": "invoke_capability",
                                    "preview": (res or "")[:200],
                                    "capability_id": "task.get_result",
                                    "phase": "task_polling",
                                    "in_progress": False,
                                }
                                ev_end["media_type"] = _extract_media_type_from_task_result(res)
                                ev_end["saved_assets"] = _extract_saved_assets_from_task_result(res)
                                logger.info("[素材] 推送 tool_end phase=task_polling(视频) saved_assets_count=%d", len(ev_end.get("saved_assets") or []))
                                await progress_cb(ev_end)
                                await progress_cb({"type": "status", "message": "正在生成回复…"})
                            except Exception:
                                pass
                elif (
                    fn.get("name") == "invoke_capability"
                    and (a.get("capability_id") or "").strip() == "task.get_result"
                    and _is_task_result_in_progress(res)
                ):
                    poll_interval = 15
                    max_wait_sec = _POLL_MAX_WAIT_GENERIC
                    task_id = (a.get("task_id") or a.get("payload", {}).get("task_id") or "").strip() if isinstance(a.get("payload"), dict) else (a.get("task_id") or "").strip()
                    logger.info("[素材] task.get_result 自动轮询开始 task_id=%s interval=%ds max_wait=%ds", task_id or "(无)", poll_interval, max_wait_sec)
                    res = await _poll_task_until_terminal_then_retry_video_on_504(
                        initial_res=res,
                        get_result_args_base=dict(a),
                        token=token,
                        sutui_token=sutui_token,
                        progress_cb=progress_cb,
                        db=db,
                        user_id=user_id,
                        tool_fn_name=fn.get("name", "invoke_capability"),
                        poll_interval=poll_interval,
                        max_wait_sec=max_wait_sec,
                        enable_504_video_retry=True,
                        video_resubmit_args=None,
                        log_label="素材] task.get_result",
                    )
                    if progress_cb:
                        try:
                            ev_end = {
                                "type": "tool_end",
                                "name": fn.get("name", ""),
                                "preview": (res or "")[:200],
                                "capability_id": "task.get_result",
                                "phase": "task_polling",
                                "in_progress": False,
                            }
                            ev_end["media_type"] = _extract_media_type_from_task_result(res)
                            ev_end["saved_assets"] = _extract_saved_assets_from_task_result(res)
                            logger.info("[素材] 推送 tool_end phase=task_polling(显式) saved_assets_count=%d", len(ev_end.get("saved_assets") or []))
                            await progress_cb(ev_end)
                            await progress_cb({"type": "status", "message": "正在生成回复…"})
                        except Exception:
                            pass
                cur.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": res,
                })
            continue

        content = (msg.get("content") or "").strip()
        logger.info("[CHAT] rnd=%d no tool_calls, content_len=%d", rnd, len(content))

        text_calls = _parse_text_tool_calls(content) if content else []
        if text_calls and rnd < MAX_TOOL_ROUNDS - 1:
            logger.info("[CHAT] parsed %d text-embedded tool calls (round %d)", len(text_calls), rnd)
            preamble = _strip_dsml(content)
            cur.append({"role": "assistant", "content": preamble or "正在调用工具..."})
            results = []
            for tc_info in text_calls:
                _inject_video_media_urls(tc_info["arguments"], attachment_urls or [])
                logger.info("[CHAT] text_tool_call: %s(%s)", tc_info["name"], list(tc_info["arguments"].keys()))
                cap = (tc_info["arguments"].get("capability_id") or "").strip()
                if cap in ("image.generate", "video.generate", "task.get_result"):
                    logger.info("[素材] 模型请求工具(text_calls) capability_id=%s", cap)
                res = await _exec_tool_with_balance(
                    tc_info["name"], tc_info["arguments"], token, sutui_token, progress_cb, db, user_id,
                )
                if (
                    tc_info["name"] == "invoke_capability"
                    and cap == "image.generate"
                    and _is_task_result_in_progress(res)
                ):
                    task_id = _extract_task_id_from_result(res)
                    if task_id:
                        get_result_args = {"capability_id": "task.get_result", "payload": {"task_id": task_id}}
                        poll_interval = 15
                        max_wait_sec = _POLL_MAX_WAIT_IMAGE
                        res = await _poll_task_until_terminal_then_retry_video_on_504(
                            initial_res=res,
                            get_result_args_base=get_result_args,
                            token=token,
                            sutui_token=sutui_token,
                            progress_cb=progress_cb,
                            db=db,
                            user_id=user_id,
                            tool_fn_name=tc_info["name"],
                            poll_interval=poll_interval,
                            max_wait_sec=max_wait_sec,
                            enable_504_video_retry=False,
                            video_resubmit_args=None,
                            log_label="素材] image.generate(text_calls)",
                        )
                        if progress_cb:
                            try:
                                ev_end = {
                                    "type": "tool_end",
                                    "name": tc_info["name"],
                                    "preview": (res or "")[:200],
                                    "capability_id": "task.get_result",
                                    "phase": "task_polling",
                                    "in_progress": False,
                                }
                                ev_end["media_type"] = _extract_media_type_from_task_result(res)
                                ev_end["saved_assets"] = _extract_saved_assets_from_task_result(res)
                                logger.info("[素材] 推送 tool_end phase=task_polling(图片,text_calls) saved_assets_count=%d", len(ev_end.get("saved_assets") or []))
                                await progress_cb(ev_end)
                                await progress_cb({"type": "status", "message": "正在生成回复…"})
                            except Exception:
                                pass
                elif (
                    tc_info["name"] == "invoke_capability"
                    and cap == "video.generate"
                    and _is_task_result_in_progress(res)
                ):
                    task_id = _extract_task_id_from_result(res)
                    if task_id:
                        get_result_args = {"capability_id": "task.get_result", "payload": {"task_id": task_id}}
                        poll_interval = 15
                        max_wait_sec = _POLL_MAX_WAIT_VIDEO
                        video_args = copy.deepcopy(tc_info["arguments"])
                        res = await _poll_task_until_terminal_then_retry_video_on_504(
                            initial_res=res,
                            get_result_args_base=get_result_args,
                            token=token,
                            sutui_token=sutui_token,
                            progress_cb=progress_cb,
                            db=db,
                            user_id=user_id,
                            tool_fn_name=tc_info["name"],
                            poll_interval=poll_interval,
                            max_wait_sec=max_wait_sec,
                            enable_504_video_retry=True,
                            video_resubmit_args=video_args,
                            log_label="素材] video.generate(text_calls)",
                        )
                        if progress_cb:
                            try:
                                ev_end = {
                                    "type": "tool_end",
                                    "name": tc_info["name"],
                                    "preview": (res or "")[:200],
                                    "capability_id": "task.get_result",
                                    "phase": "task_polling",
                                    "in_progress": False,
                                }
                                ev_end["media_type"] = _extract_media_type_from_task_result(res)
                                ev_end["saved_assets"] = _extract_saved_assets_from_task_result(res)
                                logger.info("[素材] 推送 tool_end phase=task_polling(视频,text_calls) saved_assets_count=%d", len(ev_end.get("saved_assets") or []))
                                await progress_cb(ev_end)
                                await progress_cb({"type": "status", "message": "正在生成回复…"})
                            except Exception:
                                pass
                elif (
                    tc_info["name"] == "invoke_capability"
                    and (tc_info["arguments"].get("capability_id") or "").strip() == "task.get_result"
                    and _is_task_result_in_progress(res)
                ):
                    poll_interval = 15
                    max_wait_sec = _POLL_MAX_WAIT_GENERIC
                    args = tc_info["arguments"]
                    task_id = (args.get("task_id") or ((args.get("payload") or {}).get("task_id") if isinstance(args.get("payload"), dict) else None) or "").strip()
                    logger.info("[素材] task.get_result 自动轮询开始(text_calls) task_id=%s interval=%ds", task_id or "(无)", poll_interval)
                    res = await _poll_task_until_terminal_then_retry_video_on_504(
                        initial_res=res,
                        get_result_args_base=dict(args),
                        token=token,
                        sutui_token=sutui_token,
                        progress_cb=progress_cb,
                        db=db,
                        user_id=user_id,
                        tool_fn_name=tc_info["name"],
                        poll_interval=poll_interval,
                        max_wait_sec=max_wait_sec,
                        enable_504_video_retry=True,
                        video_resubmit_args=None,
                        log_label="素材] task.get_result(text_calls)",
                    )
                    if progress_cb:
                        try:
                            ev_end = {
                                "type": "tool_end",
                                "name": tc_info["name"],
                                "preview": (res or "")[:200],
                                "capability_id": "task.get_result",
                                "phase": "task_polling",
                                "in_progress": False,
                            }
                            ev_end["media_type"] = _extract_media_type_from_task_result(res)
                            ev_end["saved_assets"] = _extract_saved_assets_from_task_result(res)
                            logger.info("[素材] 推送 tool_end phase=task_polling(显式,text_calls) saved_assets_count=%d", len(ev_end.get("saved_assets") or []))
                            await progress_cb(ev_end)
                            await progress_cb({"type": "status", "message": "正在生成回复…"})
                        except Exception:
                            pass
                results.append(f"[{tc_info['name']}] {res}")
            cur.append({"role": "user", "content": "工具调用结果:\n" + "\n\n".join(results) + "\n\n请根据以上结果回答用户。"})
            continue

        # 用户明确要求执行操作、且助手只回了文字没调工具时，强制要求调工具（不因「已成功」等总结语放过）
        last_user_msg = ""
        for m in reversed(cur):
            if m.get("role") == "user":
                last_user_msg = (m.get("content") or "").strip()
                break
        if (
            oai_tools
            and rnd == 0
            and not force_tool_retry_done
            and _ACTION_KW.search(content)
            and _USER_ACTION_KW.search(last_user_msg)
        ):
            logger.warning(
                "[CHAT] LLM replied with action text but NO tool_call (user asked for action). "
                "Retrying with tool_choice=required. Content preview: %s",
                content[:200],
            )
            force_tool_retry_done = True
            cur.append({"role": "assistant", "content": content})
            cur.append({
                "role": "user",
                "content": (
                    "你刚才只回复了文字，没有调用任何工具。"
                    "请立即调用对应的工具来执行操作（如 publish_content、invoke_capability、open_account_browser 等），"
                    "不要只用文字描述。"
                ),
            })
            body_retry: Dict[str, Any] = {
                "model": model, "messages": cur, "stream": False,
                "tools": oai_tools, "tool_choice": "required",
            }
            async with httpx.AsyncClient(timeout=120.0) as c:
                resp2 = await c.post(url, json=body_retry, headers=hdrs)
            if resp2.status_code == 200:
                choice2 = (resp2.json().get("choices") or [{}])[0]
                msg2 = choice2.get("message", {})
                tcs2 = msg2.get("tool_calls", [])
                if tcs2:
                    logger.info("[CHAT] forced retry produced %d tool_calls", len(tcs2))
                    cur.append(msg2)
                    for tc in tcs2:
                        fn = tc.get("function", {})
                        try:
                            a = json.loads(fn.get("arguments", "{}"))
                        except Exception:
                            a = {}
                        logger.info("[CHAT] tool_call(forced): %s(%s)", fn.get("name"), list(a.keys()))
                        res = await _exec_tool_with_balance(
                            fn.get("name", ""), a, token, sutui_token, progress_cb, db, user_id,
                        )
                        cap_f = (a.get("capability_id") or "").strip()
                        if (
                            fn.get("name") == "invoke_capability"
                            and cap_f == "image.generate"
                            and _is_task_result_in_progress(res)
                        ):
                            task_id = _extract_task_id_from_result(res)
                            if task_id:
                                get_result_args = {"capability_id": "task.get_result", "payload": {"task_id": task_id}}
                                poll_interval = 15
                                max_wait_sec = _POLL_MAX_WAIT_IMAGE
                                logger.info("[素材] image.generate 自动轮询开始(forced) task_id=%s", task_id)
                                res = await _poll_task_until_terminal_then_retry_video_on_504(
                                    initial_res=res,
                                    get_result_args_base=get_result_args,
                                    token=token,
                                    sutui_token=sutui_token,
                                    progress_cb=progress_cb,
                                    db=db,
                                    user_id=user_id,
                                    tool_fn_name="invoke_capability",
                                    poll_interval=poll_interval,
                                    max_wait_sec=max_wait_sec,
                                    enable_504_video_retry=False,
                                    video_resubmit_args=None,
                                    log_label="素材] image.generate(forced)",
                                )
                                if progress_cb:
                                    try:
                                        saved = _extract_saved_assets_from_task_result(res)
                                        ev_end = {
                                            "type": "tool_end",
                                            "name": "invoke_capability",
                                            "preview": (res or "")[:200],
                                            "capability_id": "task.get_result",
                                            "phase": "task_polling",
                                            "in_progress": False,
                                            "media_type": _extract_media_type_from_task_result(res),
                                            "saved_assets": saved,
                                        }
                                        await progress_cb(ev_end)
                                        await progress_cb({"type": "status", "message": "正在生成回复…"})
                                    except Exception:
                                        pass
                        elif (
                            fn.get("name") == "invoke_capability"
                            and cap_f == "video.generate"
                            and _is_task_result_in_progress(res)
                        ):
                            task_id = _extract_task_id_from_result(res)
                            if task_id:
                                get_result_args = {"capability_id": "task.get_result", "payload": {"task_id": task_id}}
                                poll_interval = 15
                                max_wait_sec = _POLL_MAX_WAIT_VIDEO
                                video_args = copy.deepcopy(a)
                                logger.info("[素材] video.generate 自动轮询开始(forced) task_id=%s", task_id)
                                res = await _poll_task_until_terminal_then_retry_video_on_504(
                                    initial_res=res,
                                    get_result_args_base=get_result_args,
                                    token=token,
                                    sutui_token=sutui_token,
                                    progress_cb=progress_cb,
                                    db=db,
                                    user_id=user_id,
                                    tool_fn_name="invoke_capability",
                                    poll_interval=poll_interval,
                                    max_wait_sec=max_wait_sec,
                                    enable_504_video_retry=True,
                                    video_resubmit_args=video_args,
                                    log_label="素材] video.generate(forced)",
                                )
                                if progress_cb:
                                    try:
                                        saved = _extract_saved_assets_from_task_result(res)
                                        ev_end = {
                                            "type": "tool_end",
                                            "name": "invoke_capability",
                                            "preview": (res or "")[:200],
                                            "capability_id": "task.get_result",
                                            "phase": "task_polling",
                                            "in_progress": False,
                                            "media_type": _extract_media_type_from_task_result(res),
                                            "saved_assets": saved,
                                        }
                                        await progress_cb(ev_end)
                                        await progress_cb({"type": "status", "message": "正在生成回复…"})
                                    except Exception:
                                        pass
                        cur.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": res,
                        })
                    continue
                else:
                    logger.warning("[CHAT] forced retry still no tool_calls")

        if oai_tools and rnd == 0:
            _FAKE_PATTERN = re.compile(
                r"(已为你|已成功|已生成|已发布|发布成功|生成完成|"
                r"!\[.*\]\(https?://|https?://.*\.(jpg|png|mp4))",
                re.IGNORECASE,
            )
            if _FAKE_PATTERN.search(content):
                logger.warning("[CHAT] possible fabricated result (no tools called): %s", content[:300])
                try:
                    logs = _pending_tool_logs.get()
                except LookupError:
                    logs = []
                if not logs:
                    content += "\n\n⚠️ 注意：以上回复可能是AI模拟的结果，并非真实执行。如需真正执行操作，请再次明确告诉我。"

        return _reply_for_user(content)

    return "（工具调用轮数已达上限）"


async def _chat_anthropic(
    msgs: List[Dict],
    cfg: Dict,
    mcp_tools: List[Dict],
    token: str,
    sutui_token: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    attachment_urls: Optional[List[str]] = None,
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> str:
    """Anthropic Messages API chat loop."""
    hdrs = {
        "Content-Type": "application/json",
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
    }
    sys_text = ""
    ant_msgs: List[Dict] = []
    for m in msgs:
        if m["role"] == "system":
            sys_text = m["content"]
        elif m["role"] in ("user", "assistant"):
            ant_msgs.append({"role": m["role"], "content": m["content"]})

    ant_tools = [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
        }
        for t in mcp_tools
    ]

    for rnd in range(MAX_TOOL_ROUNDS):
        body: Dict[str, Any] = {
            "model": cfg["model_name"],
            "max_tokens": 4096,
            "messages": ant_msgs,
        }
        if sys_text:
            body["system"] = sys_text
        if ant_tools and rnd < MAX_TOOL_ROUNDS - 1:
            body["tools"] = ant_tools

        async with httpx.AsyncClient(timeout=120.0) as c:
            resp = await c.post(
                "https://api.anthropic.com/v1/messages", json=body, headers=hdrs,
            )
        if resp.status_code != 200:
            _raise_api_err(resp, model="anthropic/" + cfg.get("model_name", ""))

        data = resp.json()
        blocks = data.get("content", [])
        tus = [b for b in blocks if b.get("type") == "tool_use"]

        if tus and data.get("stop_reason") == "tool_use":
            ant_msgs.append({"role": "assistant", "content": blocks})
            results = []
            for tu in tus:
                inp = tu.get("input") if isinstance(tu.get("input"), dict) else {}
                if not inp:
                    tu["input"] = inp
                _inject_video_media_urls(inp, attachment_urls or [])
                logger.info("tool_call: %s", tu["name"])
                r = await _exec_tool_with_balance(
                    tu["name"], inp, token, sutui_token, progress_cb, db, user_id,
                )
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": r,
                })
            ant_msgs.append({"role": "user", "content": results})
            continue

        text_parts = [
            b.get("text", "") for b in blocks if b.get("type") == "text"
        ]
        return "\n".join(text_parts).strip() or "（无回复内容）"

    return "（工具调用轮数已达上限）"


# ── OpenClaw Gateway fallback ─────────────────────────────────────

async def _try_openclaw(
    msgs: List[Dict], model: str, raw_token: str,
) -> Optional[str]:
    """Attempt to get a reply via OpenClaw Gateway. Returns None on failure."""
    oc_base = (settings.openclaw_gateway_url or "").strip().rstrip("/")
    oc_token = (settings.openclaw_gateway_token or "").strip()
    if not oc_base or not oc_token:
        return None

    agent_id = "main"
    if model and "/" in model:
        slug = re.sub(
            r'[^a-z0-9_-]', '-',
            model.lower().replace("/", "-").replace(".", "-"),
        )
        agent_id = re.sub(r'-+', '-', slug).strip('-')[:64] or "main"

    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            resp = await c.post(
                f"{oc_base}/v1/chat/completions",
                json={"model": model, "messages": msgs, "stream": False},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {oc_token}",
                    "x-openclaw-agent-id": agent_id,
                    "x-user-authorization": f"Bearer {raw_token}",
                },
            )
        if resp.status_code == 200:
            choices = resp.json().get("choices", [])
            if choices:
                return (choices[0].get("message", {}).get("content") or "").strip()
    except Exception:
        pass
    return None


_CHANNELS_REQUIRE_BINDING = frozenset({"wecom", "wechat_oa"})


def resolve_billing_user_for_channel(db: Session, *, channel: str, from_user: str) -> Optional[User]:
    """微信/企微渠道：按回调身份解析已绑定的站内用户（用于 JWT、MCP 扣费与 x-user-authorization）。"""
    fu = (from_user or "").strip()
    ch = (channel or "").strip().lower()
    if not fu or not ch:
        return None
    if ch == "wecom":
        return db.query(User).filter(User.wecom_userid == fu).first()
    if ch == "wechat_oa":
        return db.query(User).filter(User.wechat_openid == fu).first()
    return None


_CHANNEL_UNBOUND_MSG = (
    "该微信渠道账号未绑定本系统用户，无法使用 AI 能力与算力扣费。"
    "企业微信：请登录本系统后在「账号」中绑定企业微信 UserID（与回调中的发送者 ID 一致）；"
    "微信服务号：请使用微信扫码登录，使服务号 openid 与账号关联。"
)


async def get_reply_for_channel(
    user_message: str,
    session_id: str = "",
    system_prompt_extra: str = "",
    *,
    channel_system: str = "",
    channel: str = "",
    from_user: str = "",
) -> str:
    """供企业微信/Messenger/服务号等渠道回调使用：仅文本入、文本出。
    企业微信(wecom)、服务号(wechat_oa)须已绑定本站用户，才会携带用户 JWT 拉 MCP 工具并走预扣/结算；否则返回绑定提示。
    未传 channel/from_user 时（如 Messenger 旧逻辑）保持匿名，不向 MCP 传用户凭证（与历史行为一致）。"""
    if not (user_message or "").strip():
        return "收到。"
    ch = (channel or "").strip().lower()
    fu = (from_user or "").strip()
    need_binding = ch in _CHANNELS_REQUIRE_BINDING

    db = SessionLocal()
    try:
        user: Optional[User] = None
        if ch and fu:
            user = resolve_billing_user_for_channel(db, channel=ch, from_user=fu)
        if need_binding and not user:
            return _CHANNEL_UNBOUND_MSG

        raw_token = ""
        sutui_token: Optional[str] = None
        uid: Optional[int] = None
        if user is not None:
            raw_token = create_access_token(data=access_token_claims(user))
            sutui_token = (getattr(user, "sutui_token", None) or "").strip() or None
            uid = user.id

        mcp_tools = await _fetch_mcp_tools(raw_token)

        model = ""
        try:
            model = _pick_default_model()
        except HTTPException:
            model = "openclaw"
        cfg = _resolve_config(model) if model else None
        base_role = (channel_system or "").strip() or "你是企业微信客服助手。根据用户消息简短、友好地回复。使用中文。"
        sys = base_role + (("\n" + system_prompt_extra.strip()) if system_prompt_extra else "")
        messages = [
            {"role": "system", "content": sys},
            {"role": "user", "content": (user_message or "").strip()},
        ]
        if cfg:
            try:
                if cfg.get("provider") == "anthropic":
                    reply = await _chat_anthropic(
                        messages, cfg, mcp_tools, raw_token,
                        sutui_token=sutui_token,
                        db=db, user_id=uid,
                    )
                else:
                    reply = await _chat_openai(
                        messages, cfg, mcp_tools, raw_token,
                        sutui_token=sutui_token,
                        db=db, user_id=uid,
                    )
                return (reply or "").strip() or "收到。"
            except HTTPException:
                return "服务暂时不可用，请稍后再试。"
            except Exception as e:
                logger.exception("[渠道回复] chat 异常: %s", e)
                return "处理时遇到问题，请稍后再试。"
        oc_reply = await _try_openclaw(messages, model or "openclaw", raw_token)
        if oc_reply:
            return (oc_reply or "").strip() or "收到。"
        return "抱歉，当前未配置对话模型或 OpenClaw，无法回复。"
    finally:
        db.close()


# ── Chat turn logging ─────────────────────────────────────────────

def _flush_tool_logs(db: Session, uid: int, session_id: Optional[str], model: Optional[str]):
    """Persist collected tool call records to the database."""
    try:
        logs = _pending_tool_logs.get()
    except LookupError:
        return
    for entry in logs:
        db.add(ToolCallLog(
            user_id=uid,
            tool_name=entry["tool_name"],
            arguments=entry.get("arguments"),
            result_text=entry.get("result_text"),
            result_urls=entry.get("result_urls"),
            success=entry.get("success", True),
            latency_ms=entry.get("latency_ms"),
            session_id=(session_id or "")[:128] or None,
            model=(model or "")[:128] or None,
        ))
    _pending_tool_logs.set([])


def _log_turn(
    db: Session, uid: int, user_msg: str, reply: str,
    sid: Optional[str], cid: Optional[str], meta: Optional[Dict] = None,
):
    db.add(ChatTurnLog(
        user_id=uid,
        session_id=(sid or "")[:128] or None,
        context_id=(cid or "")[:128] or None,
        user_message=(user_msg or "")[:5000],
        assistant_reply=(reply or "")[:20000],
        meta=meta or {},
    ))


# ── 附图 URL：与 lobster_online 一致，仅 TOS/upload-temp 等公网 source_url；禁止 /api/assets/file/ 签名链 ──

_MAX_VIDEO_IMAGE_ATTACHMENTS = 9
_LOCAL_SIGNED_ASSET_PATH = "/api/assets/file/"


def _ensure_upstream_image_url(u: str, label: str) -> str:
    if _LOCAL_SIGNED_ASSET_PATH in u:
        logger.error("[使用素材-失败] %s 命中已禁止签名链 /api/assets/file/", label)
        raise HTTPException(
            status_code=400,
            detail=f"{label} 不可用于图生视频（旧版签名链接）。请使用 TOS 或临时上传返回的公网 URL。",
        )
    return u


def _resolve_asset_ids_to_public_urls(
    attachment_asset_ids: Optional[List[str]],
    request: Request,
    db,
    user_id: int,
) -> List[str]:
    from backend.app.api.assets import get_asset_public_url

    aids = [
        a.strip()
        for a in (attachment_asset_ids or [])[:_MAX_VIDEO_IMAGE_ATTACHMENTS]
        if isinstance(a, str) and a.strip()
    ]
    if not aids:
        return []
    out: List[str] = []
    for aid in aids:
        logger.info("[使用素材-步骤B.1] 开始处理素材 asset_id=%s", aid)
        u = get_asset_public_url(aid, user_id, request, db)
        if not u:
            logger.error(
                "[使用素材-失败] asset_id=%s 无公网 source_url（服务器 chat 仅认 DB 中 TOS 等外链）",
                aid,
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"素材 {aid} 没有公网可访问链接。"
                    "请在服务器配置 TOS 后上传，或使用 lobster_online 本机上传（本机 TOS → upload-temp）。"
                ),
            )
        _ensure_upstream_image_url(u, f"asset {aid}")
        logger.info("[使用素材-步骤B.2] 公网 URL 已解析 asset_id=%s url=%s", aid, u[:80])
        out.append(u)
    logger.info("[使用素材-步骤B.5] 附图 asset 公网 URL 齐全 count=%d asset_ids=%s", len(out), aids)
    return out


# ── Main endpoint ─────────────────────────────────────────────────

def _build_user_content_with_attachments(
    payload: ChatRequest,
    request: Optional[Request] = None,
    db=None,
    user_id: Optional[int] = None,
) -> str:
    user_content = (payload.message or "").strip()
    if getattr(payload, "attachment_asset_ids", None) and request and db is not None and user_id is not None:
        from backend.app.api.assets import get_asset_public_url

        pairs = []
        aids = [a.strip() for a in (payload.attachment_asset_ids or [])[:5] if isinstance(a, str) and a.strip()]
        for aid in aids:
            u = get_asset_public_url(aid, user_id, request, db)
            if not u:
                logger.error("[CHAT] 附图无公网 URL，中止组装用户消息 asset_id=%s", aid)
                raise HTTPException(
                    status_code=400,
                    detail=f"素材 {aid} 没有公网可访问链接，无法继续对话中的图/视频任务。",
                )
            _ensure_upstream_image_url(u, f"asset {aid}")
            pairs.append((aid, u))
        if pairs:
            logger.info("[CHAT] 注入素材 URL: asset_ids=%s", [p[0] for p in pairs])
            user_content += (
                "\n\n【用户本条消息上传的素材】\n"
                "- 图生视频：你不要在 video.generate 的 payload 里填 image_url/media_files，由系统自动注入。\n"
                "- 图生图 / 图片编辑：调用 image.generate，在 payload 里设 image_url 为下方 URL，并指定编辑模型（wan/v2.7/edit、seedream/v4.5/edit 等）。\n"
                "- 理解图片：调用 image.understand，在 payload 里设 image_url 为下方 URL，prompt 为用户的要求。\n"
                "- 理解视频：调用 video.understand，在 payload 里设 video_url 为下方 URL，prompt 为用户的要求。\n"
            )
            user_content += "\n".join(f"- asset_id: {aid}  URL: {u}" for aid, u in pairs)
    return user_content or "（无文字）"


def _get_attachment_public_urls(
    payload: ChatRequest,
    request: Optional[Request],
    db=None,
    user_id: Optional[int] = None,
) -> List[str]:
    """attachment_image_urls（显式公网图链）+ asset_ids（DB source_url）。禁止 /api/assets/file/。"""
    out: List[str] = []
    if getattr(payload, "attachment_image_urls", None):
        for raw in (payload.attachment_image_urls or [])[:_MAX_VIDEO_IMAGE_ATTACHMENTS]:
            if isinstance(raw, str) and (u := raw.strip()) and u.startswith("http"):
                _ensure_upstream_image_url(u, "attachment_image_urls")
                out.append(u)
    if getattr(payload, "attachment_asset_ids", None) and request and db is not None and user_id is not None:
        more = _resolve_asset_ids_to_public_urls(
            getattr(payload, "attachment_asset_ids", None), request, db, user_id
        )
        for u in more:
            if u not in out:
                out.append(u)
    if out:
        logger.info("[CHAT] 图生视频可用 URL 数量=%d 首图=%s", len(out), (out[0][:80] + "…") if len(out[0]) > 80 else out[0])
    return out


def _inject_video_media_urls(args: Dict[str, Any], attachment_urls: List[str]) -> None:
    """video.generate 时：注入图片 URL（filePaths/media_files/image_url）并确保 prompt 含「图生视频」文案，与 lobster 单机版一致。"""
    if not args or (args.get("capability_id") or "").strip() != "video.generate":
        return
    inner = args.get("payload")
    if not isinstance(inner, dict):
        inner = {}
        args["payload"] = inner
    if attachment_urls:
        urls = list(attachment_urls)[:9]
        inner["filePaths"] = urls
        inner["functionMode"] = "first_last_frames"
        inner["media_files"] = urls
        inner["image_url"] = urls[0]
        logger.info("[CHAT] 图生视频注入 filePaths（%d 张）functionMode=first_last_frames 首图=%s", len(urls), (urls[0][:80] + "…") if len(urls[0]) > 80 else urls[0])
    elif not inner.get("media_files") and not inner.get("image_url") and not inner.get("filePaths"):
        logger.warning(
            "[CHAT] 图生视频未注入链接：附图为 0 个公网 URL；若用户已附图应已在对话入口被 400 拦截，此处多为文生视频"
        )
    existing = (inner.get("prompt") or "").strip()
    if "图生视频" not in existing:
        inner["prompt"] = ("图生视频：" + existing) if existing else "图生视频"
        logger.info("[CHAT] 图生视频 prompt 补全「图生视频」前缀")


@router.post("/chat", response_model=ChatResponse, summary="智能对话")
async def chat_endpoint(
    request: Request,
    payload: ChatRequest,
    raw_token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _pending_tool_logs.set([])
    logger.info(
        "[素材] 对话开始(POST /chat) session_id=%s message_len=%d attachment_count=%d",
        getattr(payload, "session_id", None) or "",
        len((payload.message or "").strip()),
        len(getattr(payload, "attachment_asset_ids", None) or []),
    )

    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition == "online":
        model = "sutui"
        # 速推能力由服务器转发，使用服务器侧 Token，不传用户 token
        sutui_token = None
    else:
        sutui_token = None
        model = payload.model or getattr(current_user, "preferred_model", None) or ""
        if not model or model == "openclaw":
            model = _pick_default_model()

    mcp_tools = await _fetch_mcp_tools(raw_token)
    has_tools = bool(mcp_tools)
    if not has_tools:
        logger.info(
            "MCP tools empty (port 8001 may be down or unreachable), chat has no capabilities; "
            "user will see 'cannot generate image'. Check that MCP service is started (run_mcp.bat / start.bat)."
        )
    # 在线版：对话用首个已配置的模型（无则走 OpenClaw），能力走速推（sutui_token）
    if edition == "online":
        try:
            resolve_model = _pick_default_model()
        except HTTPException:
            resolve_model = None
    else:
        resolve_model = model

    sys_prompt = (
        "你是「龙虾」(Lobster)，用户的私人 AI 助手。"
        "龙虾内置了「速推 MCP」能力：文生图、图生视频、语音合成、视频解析等，具体以 list_capabilities 返回为准。\n\n"
        + (
            "【绝对禁止 — 违反将导致严重错误】\n"
            "1. 禁止模拟/伪造工具调用结果。你没有能力直接生成图片、发布内容、操作浏览器。你只能通过调用工具来做这些事。\n"
            "2. 禁止编造 URL、asset_id、图片链接、发布结果。所有数据必须来自工具返回。\n"
            "3. 禁止在回复中假装已经完成了操作（如\"已为你生成\"\"已发布成功\"）而实际没有调用工具。\n"
            "4. 禁止用文字描述代替工具调用。说\"好的，我来发布\"然后不调用工具 = 严重错误。\n"
            "5. 当用户要求执行操作时，你必须在本轮回复中调用工具，不能只回复文字。\n\n"
            "【正确做法】\n"
            "- 用户问「你能做什么」「速推能力」「MCP 能力」「内置能力」等 → 必须先调用 list_capabilities，根据返回的 capability 列表和描述如实回答，不要用通用话术。\n"
            "- 用户要你做事 → 立即调用对应的工具函数，等工具返回真实结果后再回复用户。\n"
            "- 不确定该调哪个工具 → 先调用 list_capabilities 或 list_assets 查询，不要猜测。\n"
            "- 工具调用失败 → 如实告诉用户失败原因，不要编造成功结果。\n\n"
            "【工具使用指南】\n"
            "生成图片：invoke_capability(capability_id=\"image.generate\", payload={\"prompt\":\"...\", \"model\":\"jimeng-4.0\"})\n"
            "  → 返回 task_id 后用 task.get_result(task_id) 取结果，结果中 saved_assets[0].asset_id 为素材ID\n"
            "【图片 prompt 规则】用用户原话作 prompt（去掉指令性表述如「发布到某账号」等），不要改写、不要臆造；仅当用户明确说「让ai写词」「你写词」「帮我写提示词」时才由你撰写 prompt。\n"
            "生成视频：文生视频 invoke_capability(capability_id=\"video.generate\", payload={\"model\":\"st-ai/super-seed2\", \"prompt\":\"...\", \"duration\":5})\n"
            "  图生视频：当用户本条消息带有上传的图片或附图为「生成的图」时，你只需填写 prompt、model、duration 等，**禁止**在 payload 中填写 image_url 或 media_files（系统会根据用户附图/上一轮生成图自动注入）；无附图时若用户指定了某张图/素材，按速推要求填 image_url 等。\n"
            "【视频 prompt 规则】video.generate 的 prompt 里**只放「生成内容描述」**，其余一律不进 prompt。\n"
            "  不进 prompt 的（无论出现在句子的前面、中间还是后面都要去掉）：① 条件（如「用速推的」「用 wan/v2.6/image-to-video」「用某模型」「用上传的图片」）→ 只影响 payload.model 或由系统注入图；② 动作（如「发布到抖音」「发到某账号」「上传小红书」）→ 只触发 publish_content。\n"
            "  只进 prompt 的：用户说的「生成什么」的表述（如「生成一个产品视频，产品名字a」）。先去掉上述条件与动作，**剩余原文作为 prompt，不要改写、不要臆造**。仅当用户明确说「让ai写词」「你写词」「帮我写提示词」时才由你撰写 prompt。用户提到用某模型时，payload.model 填对应模型（如 wan/v2.6/image-to-video、st-ai/super-seed2）。\n"
            "  → 返回 task_id 后必须调 task.get_result(task_id) 轮询，视频通常 30–120 秒完成\n"
            "生成并发布：先 invoke_capability 生成 → 拿 asset_id → 调 publish_content\n"
            "当用户要求「发到某账号」「发布到抖音」「上传小红书」等时，在 task.get_result 返回成功（含 saved_assets 或 result 中的 asset_id）后，立即调用 publish_content(asset_id, account_nickname, ...)，无需等用户再次确认或点击。\n"
            "【图片编辑 / 图生图】用户上传图片并说「编辑」「改成…风格」「换背景」「把图片变成…」时，调用 image.generate，payload 中 image_url 填附件 URL，model 填编辑模型：\n"
            "  - wan/v2.7/edit（标准）、wan/v2.7/pro/edit（增强）：prompt 中用 image 1 引用输入图\n"
            "  - fal-ai/bytedance/seedream/v4.5/edit、fal-ai/bytedance/seedream/v5/lite/edit：prompt 中用 Figure 1 引用\n"
            "  - fal-ai/qwen-image-edit-2511-multiple-angles：多角度生成\n"
            "  → 与文生图一样返回 task_id，用 task.get_result 取结果\n"
            "【理解图片】用户上传图片并说「理解一下」「描述」「这张图是什么」「看看这张图」「识别一下」时：\n"
            "  调用 invoke_capability(capability_id=\"image.understand\", payload={\"image_url\": \"附件URL\", \"prompt\": \"用户的要求或 请详细描述这张图片的内容\"})\n"
            "  → 返回 task_id，用 task.get_result 取结果文本，融入你的回复。\n"
            "【理解视频】用户上传视频并说「这段视频讲了什么」「总结视频」「分析这个视频」时：\n"
            "  调用 invoke_capability(capability_id=\"video.understand\", payload={\"video_url\": \"附件URL\", \"prompt\": \"用户的要求或 请详细描述视频内容\"})\n"
            "  → 返回 task_id，用 task.get_result 取结果文本，融入你的回复。\n"
            "【发布约束】\n"
            "- 发布必须由你调用 publish_content 等工具完成，不得要求用户「点加号」「到发布页」「准备好后告诉我」等手动操作。\n"
            "- task.get_result 返回成功且用户要求发布时，必须**在同一轮**紧接着调用 publish_content（或先 list_publish_accounts 再 publish_content），禁止只回复「图片/视频已生成，现在去发布」「让我检查某账号是否存在」等文字而不调用工具；若需确认账号，先调 list_publish_accounts，拿到结果后立即调 publish_content。\n"
            "- 用户说「用某素材生成视频并发布到某账号」时：发布时 asset_id 必须用 task.get_result 返回的 saved_assets 中的 ID（本次生成的视频），不得用用户提供的垫图/输入素材 ID。\n"
            "- 用户说「用生成的」「发刚才生成的」「用这个生成的素材」时：即指上一轮 task.get_result 已返回结果中的 saved_assets[0].asset_id，直接调用 publish_content(该 asset_id, account_nickname, ...)，不要再次调用 video.generate 或 task.get_result。\n"
            "- 用户问「生成好了吗」「好了吗」「完成了吗」等时：若对话历史中你上一条回复已明确说明「视频已生成」或「图片已生成」且含有 saved_assets/结果链接，则**不要再次调用 task.get_result**，直接根据历史回复「已经生成好了，您可以…」并引用上条结果；仅当上一条并未返回成功结果或用户是在等待中的追问时，才可再查 task.get_result。\n"
            "- **区分「提交生成」与「查询结果」**：video.generate 是提交新的生成任务；task.get_result(task_id) 只是查询已有任务的状态/结果，不会新建任务。当你调用 task.get_result 时（例如用户问「生成好了吗」后你去查状态），回复中**禁止**使用「重新提交了任务」「任务已重新提交」「重新尝试生成」等表述，必须明确写成「正在查询您之前的任务结果」或「未重新提交，正在查询之前的任务状态」，避免用户误以为又提交了一次生成。\n"
            "- 若 invoke_capability 或 task.get_result 返回错误/失败，必须明确告知用户「本次生成失败」及原因，不得用其他素材或历史结果冒充本次生成成功；只有当前 task.get_result 明确返回成功且含 saved_assets 时，根据 saved_assets[0].media_type 回复「图片已生成」或「视频已生成」并继续发布。失败后禁止自行重试或再生成一个别的视频，只把失败原因提示给用户即可。\n"
            "发布：publish_content(asset_id, account_nickname, title, description, tags)\n"
            "打开浏览器：open_account_browser(account_nickname=\"xxx\")\n"
            "检查登录：check_account_login(account_nickname=\"xxx\")\n"
            "查素材：list_assets  查账号：list_publish_accounts\n"
            "【Instagram / Facebook 发布】\n"
            "- 查 IG/FB 账号：list_meta_social_accounts\n"
            "- 发布到 IG/FB：publish_meta_social(account_id, platform, content_type, asset_id/image_url/video_url, caption, tags)\n"
            "  Instagram 支持 photo/video/carousel/reel/story；Facebook 支持 photo/video/link\n"
            "- 查 IG/FB 数据：get_meta_social_data(account_id?, platform?) — 返回帖子列表与 Insights 指标\n"
            "- 同步最新 IG/FB 数据：sync_meta_social_data(account_id?) — 先同步再用 get_meta_social_data 读取\n"
            "- 用户问 IG/FB 表现、数据、互动率时：先 sync 再 get，用返回 JSON 直接分析，勿编造数字\n"
            "【素材指代与确认】\n"
            "用户说「用我上传的」「刚才那张」「上次传的」「上次生成的」「用这张图」时，可能指：本条消息附带的图、本会话中之前某条消息附带的图、或 list_assets 返回的最近素材。若不唯一或无法确定指哪张，禁止猜测。\n"
            "正确做法：先 list_assets（或结合本条消息已附带的素材）得到候选；若有多个候选或不确定，在回复中列出每条候选的「素材 ID」和「图片/视频链接」（便于用户点击查看），并明确问用户「您指的是哪一张？是不是这张？」待用户确认后再用该 asset_id 继续生成或发布。\n"
            "禁止在未确认时随意选用 list_assets 中的第一条或某一条。\n"
            if has_tools
            else (
                "\n【当前无可用工具】能力服务(MCP 端口 8001)未就绪。"
                "若用户要求生成图片、视频、发布等，请回复：当前无法使用速推能力，请确认 (1) 已用 start.bat 或 start_headless.bat 启动完整服务（含 MCP）；(2) 管理员已在服务器配置 SUTUI_SERVER_TOKEN 或 SUTUI_SERVER_TOKENS。"
                "可访问 http://本机IP:8000/api/health 查看 mcp.reachable 与 mcp.tools_count。\n\n"
            )
        )
        + "回答使用中文，简洁友好。"
        + " 当用户发送新的短消息（如问候、新问题）时，请直接针对该新消息简短回复，不要重复或延续上一条长回复的内容。"
    )

    messages: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    for m in (payload.history or []):
        if m.role in ("user", "assistant") and (m.content or "").strip():
            content = m.content.strip()
            if len(content) > MAX_HISTORY_MESSAGE_CHARS:
                content = (
                    content[: MAX_HISTORY_MESSAGE_CHARS // 2].rstrip()
                    + "\n\n...(上条内容已省略，请根据用户新消息直接回复。)"
                )
            messages.append({"role": m.role, "content": content})
    if len(messages) > MAX_HISTORY + 1:
        messages = [messages[0]] + messages[-MAX_HISTORY:]
    messages.append({"role": "user", "content": _build_user_content_with_attachments(payload, request, db=db, user_id=current_user.id)})

    attachment_urls = _get_attachment_public_urls(payload, request, db=db, user_id=current_user.id)

    t0 = time.perf_counter()

    # ── Primary path: direct LLM API with MCP tools ──
    cfg = _resolve_config(resolve_model) if resolve_model else None
    if cfg:
        try:
            logger.info("[对话] 请求 model=%s tools=%d", model, len(mcp_tools))
            if cfg["provider"] == "anthropic":
                reply = await _chat_anthropic(
                    messages, cfg, mcp_tools, raw_token, sutui_token=sutui_token,
                    attachment_urls=attachment_urls,
                    db=db, user_id=current_user.id,
                )
            else:
                reply = await _chat_openai(
                    messages, cfg, mcp_tools, raw_token, sutui_token=sutui_token,
                    attachment_urls=attachment_urls,
                    db=db, user_id=current_user.id,
                )

            ms = round((time.perf_counter() - t0) * 1000)
            _flush_tool_logs(db, current_user.id, payload.session_id, model)
            _log_turn(
                db, current_user.id, payload.message, _reply_for_user(reply),
                payload.session_id, payload.context_id,
                {"model": model, "mode": "direct", "duration_ms": ms, "tools": len(mcp_tools)},
            )
            db.commit()
            final_reply = _reply_for_user(reply)
            logger.info("[素材] 对话结束(POST /chat) session_id=%s reply_len=%d", payload.session_id or "", len(final_reply))
            return JSONResponse(
                content=ChatResponse(reply=final_reply).model_dump(),
                headers={"X-Duration-Ms": str(ms)},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Direct LLM call failed, trying OpenClaw fallback: %s", e)

    # ── Fallback: OpenClaw Gateway (no MCP tools) ──
    oc_reply = await _try_openclaw(messages, model, raw_token)
    if oc_reply:
        ms = round((time.perf_counter() - t0) * 1000)
        _flush_tool_logs(db, current_user.id, payload.session_id, model)
        _log_turn(
            db, current_user.id, payload.message, _reply_for_user(oc_reply),
            payload.session_id, payload.context_id,
            {"model": model, "mode": "openclaw", "duration_ms": ms},
        )
        db.commit()
        final_reply = _reply_for_user(oc_reply)
        logger.info("[素材] 对话结束(POST /chat) session_id=%s reply_len=%d", payload.session_id or "", len(final_reply))
        return JSONResponse(
            content=ChatResponse(reply=final_reply).model_dump(),
            headers={"X-Duration-Ms": str(ms)},
        )

    # ── No LLM path available ──
    if not cfg:
        detail = (
            f"模型 {model} 的 API Key 未配置。"
            "请在「系统配置」中添加对应的 API Key。"
        )
    else:
        detail = "LLM 服务暂时不可用，请稍后重试。"
    raise HTTPException(status_code=503, detail=detail)


# ── Stream endpoint (SSE progress) ─────────────────────────────────

async def _chat_stream_events(
    payload: ChatRequest,
    raw_token: str,
    current_user: User,
    db: Session,
    request: Optional[Request] = None,
):
    """Async generator: yield SSE events (progress + done). Runs chat with progress_cb pushing to queue."""
    queue: asyncio.Queue = asyncio.Queue()
    reply_holder: List[str] = []
    error_holder: List[str] = []
    _request_for_assets = request

    async def progress_cb(ev: Dict) -> None:
        await queue.put(ev)

    async def run_chat() -> None:
        _pending_tool_logs.set([])
        session_id = getattr(payload, "session_id", None) or ""
        logger.info(
            "[素材] 对话开始 session_id=%s message_len=%d attachment_count=%d",
            session_id, len((payload.message or "").strip()), len(getattr(payload, "attachment_asset_ids", None) or []),
        )
        edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
        if edition == "online":
            model = "sutui"
            sutui_token = None
        else:
            sutui_token = None
            model = payload.model or getattr(current_user, "preferred_model", None) or ""
            if not model or model == "openclaw":
                model = _pick_default_model()
        mcp_tools = await _fetch_mcp_tools(raw_token)
        has_tools = bool(mcp_tools)
        if not has_tools:
            logger.info("MCP tools empty (stream path), chat has no capabilities")
        if edition == "online":
            try:
                resolve_model = _pick_default_model()
            except HTTPException:
                resolve_model = None
        else:
            resolve_model = model
        sys_prompt = (
            "你是「龙虾」(Lobster)，用户的私人 AI 助手。"
            "龙虾内置了「速推 MCP」能力：文生图、图生视频、语音合成、视频解析等，具体以 list_capabilities 返回为准。\n\n"
            + (
                "【绝对禁止 — 违反将导致严重错误】\n"
                "1. 禁止模拟/伪造工具调用结果。你没有能力直接生成图片、发布内容、操作浏览器。你只能通过调用工具来做这些事。\n"
                "2. 禁止编造 URL、asset_id、图片链接、发布结果。所有数据必须来自工具返回。\n"
                "3. 禁止在回复中假装已经完成了操作（如\"已为你生成\"\"已发布成功\"）而实际没有调用工具。\n"
                "4. 禁止用文字描述代替工具调用。说\"好的，我来发布\"然后不调用工具 = 严重错误。\n"
                "5. 当用户要求执行操作时，你必须在本轮回复中调用工具，不能只回复文字。\n\n"
                "【正确做法】\n"
                "- 用户问「你能做什么」「速推能力」「MCP 能力」「内置能力」等 → 必须先调用 list_capabilities，根据返回的 capability 列表和描述如实回答，不要用通用话术。\n"
                "- 用户要你做事 → 立即调用对应的工具函数，等工具返回真实结果后再回复用户。\n"
                "- 不确定该调哪个工具 → 先调用 list_capabilities 或 list_assets 查询，不要猜测。\n"
                "- 工具调用失败 → 如实告诉用户失败原因，不要编造成功结果。\n\n"
                "【工具使用指南】\n"
                "生成图片：invoke_capability(capability_id=\"image.generate\", payload={\"prompt\":\"...\", \"model\":\"jimeng-4.0\"})\n"
                "  → 返回 task_id 后用 task.get_result(task_id) 取结果，结果中 saved_assets[0].asset_id 为素材ID\n"
                "【图片 prompt 规则】用用户原话作 prompt（去掉指令性表述如「发布到某账号」等），不要改写、不要臆造；仅当用户明确说「让ai写词」「你写词」「帮我写提示词」时才由你撰写 prompt。\n"
                "生成视频：文生视频 invoke_capability(capability_id=\"video.generate\", payload={\"model\":\"st-ai/super-seed2\", \"prompt\":\"...\", \"duration\":5})\n"
                "  图生视频：当用户本条消息带有上传的图片或附图为「生成的图」时，你只需填写 prompt、model、duration 等，**禁止**在 payload 中填写 image_url 或 media_files（系统会根据用户附图/上一轮生成图自动注入）；无附图时若用户指定了某张图/素材，按速推要求填 image_url 等。\n"
                "【视频 prompt 规则】video.generate 的 prompt 里**只放「生成内容描述」**，其余一律不进 prompt。\n"
                "  不进 prompt 的（无论出现在句子的前面、中间还是后面都要去掉）：① 条件（如「用速推的」「用某模型」「用上传的图片」）→ 只影响 payload.model 或由系统注入图；② 动作（如「发布到抖音」「发到某账号」「上传小红书」）→ 只触发 publish_content。\n"
                "  只进 prompt 的：用户说的「生成什么」的表述（如「生成一个产品视频，产品名字a」）。先去掉上述条件与动作，**剩余原文作为 prompt，不要改写、不要臆造**。仅当用户明确说「让ai写词」「你写词」「帮我写提示词」时才由你撰写 prompt。用户提到用某模型时，payload.model 填对应模型（如 wan/v2.6/image-to-video、st-ai/super-seed2）。\n"
                "  → 返回 task_id 后必须调 task.get_result(task_id) 轮询，视频通常 30–120 秒完成\n"
                "生成并发布：先 invoke_capability 生成 → 拿 asset_id → 调 publish_content\n"
                "当用户要求「发到某账号」「发布到抖音」「上传小红书」等时，在 task.get_result 返回成功（含 saved_assets 或 result 中的 asset_id）后，立即调用 publish_content(asset_id, account_nickname, ...)，无需等用户再次确认或点击。\n"
                "【图片编辑 / 图生图】用户上传图片并说「编辑」「改成…风格」「换背景」「把图片变成…」时，调用 image.generate，payload 中 image_url 填附件 URL，model 填编辑模型：\n"
            "  - wan/v2.7/edit（标准）、wan/v2.7/pro/edit（增强）：prompt 中用 image 1 引用输入图\n"
            "  - fal-ai/bytedance/seedream/v4.5/edit、fal-ai/bytedance/seedream/v5/lite/edit：prompt 中用 Figure 1 引用\n"
            "  - fal-ai/qwen-image-edit-2511-multiple-angles：多角度生成\n"
            "  → 与文生图一样返回 task_id，用 task.get_result 取结果\n"
            "【理解图片】用户上传图片并说「理解一下」「描述」「这张图是什么」「看看这张图」「识别一下」时：\n"
            "  调用 invoke_capability(capability_id=\"image.understand\", payload={\"image_url\": \"附件URL\", \"prompt\": \"用户的要求或 请详细描述这张图片的内容\"})\n"
            "  → 返回 task_id，用 task.get_result 取结果文本，融入你的回复。\n"
            "【理解视频】用户上传视频并说「这段视频讲了什么」「总结视频」「分析这个视频」时：\n"
            "  调用 invoke_capability(capability_id=\"video.understand\", payload={\"video_url\": \"附件URL\", \"prompt\": \"用户的要求或 请详细描述视频内容\"})\n"
            "  → 返回 task_id，用 task.get_result 取结果文本，融入你的回复。\n"
                "【发布约束】\n"
                "- 发布必须由你调用 publish_content 等工具完成，不得要求用户「点加号」「到发布页」「准备好后告诉我」等手动操作。\n"
                "- task.get_result 返回成功且用户要求发布时，必须**在同一轮**紧接着调用 publish_content（或先 list_publish_accounts 再 publish_content），禁止只回复「图片/视频已生成，现在去发布」「让我检查某账号是否存在」等文字而不调用工具；若需确认账号，先调 list_publish_accounts，拿到结果后立即调 publish_content。\n"
                "- 用户说「用某素材生成视频并发布到某账号」时：发布时 asset_id 必须用 task.get_result 返回的 saved_assets 中的 ID（本次生成的视频），不得用用户提供的垫图/输入素材 ID。\n"
                "- 用户说「用生成的」「发刚才生成的」「用这个生成的素材」时：即指上一轮 task.get_result 已返回结果中的 saved_assets[0].asset_id，直接调用 publish_content(该 asset_id, account_nickname, ...)，不要再次调用 video.generate 或 task.get_result。\n"
                "- 用户问「生成好了吗」「好了吗」「完成了吗」等时：若对话历史中你上一条回复已明确说明「视频已生成」或「图片已生成」且含有 saved_assets/结果链接，则**不要再次调用 task.get_result**，直接根据历史回复「已经生成好了，您可以…」并引用上条结果；仅当上一条并未返回成功结果或用户是在等待中的追问时，才可再查 task.get_result。\n"
                "- **区分「提交生成」与「查询结果」**：video.generate 是提交新的生成任务；task.get_result(task_id) 只是查询已有任务的状态/结果，不会新建任务。当你调用 task.get_result 时（例如用户问「生成好了吗」后你去查状态），回复中**禁止**使用「重新提交了任务」「任务已重新提交」「重新尝试生成」等表述，必须明确写成「正在查询您之前的任务结果」或「未重新提交，正在查询之前的任务状态」，避免用户误以为又提交了一次生成。\n"
                "- 若 invoke_capability 或 task.get_result 返回错误/失败，必须明确告知用户「本次生成失败」及原因，不得用其他素材或历史结果冒充本次生成成功；只有当前 task.get_result 明确返回成功且含 saved_assets 时，根据 saved_assets[0].media_type 回复「图片已生成」或「视频已生成」并继续发布。失败后禁止自行重试或再生成一个别的视频，只把失败原因提示给用户即可。\n"
                "发布：publish_content(asset_id, account_nickname, title, description, tags)\n"
                "打开浏览器：open_account_browser(account_nickname=\"xxx\")\n"
                "检查登录：check_account_login(account_nickname=\"xxx\")\n"
                "查素材：list_assets  查账号：list_publish_accounts\n"
                "【素材指代与确认】\n"
                "用户说「用我上传的」「刚才那张」「上次传的」「上次生成的」「用这张图」时，可能指：本条消息附带的图、本会话中之前某条消息附带的图、或 list_assets 返回的最近素材。若不唯一或无法确定指哪张，禁止猜测。\n"
                "正确做法：先 list_assets（或结合本条消息已附带的素材）得到候选；若有多个候选或不确定，在回复中列出每条候选的「素材 ID」和「图片/视频链接」（便于用户点击查看），并明确问用户「您指的是哪一张？是不是这张？」待用户确认后再用该 asset_id 继续生成或发布。\n"
                "禁止在未确认时随意选用 list_assets 中的第一条或某一条。\n"
                if has_tools
                else (
                    "\n【当前无可用工具】能力服务(MCP 端口 8001)未就绪。"
                    "若用户要求生成图片、视频、发布等，请回复：当前无法使用速推能力，请确认 (1) 已用 start.bat 或 start_headless.bat 启动完整服务（含 MCP）；(2) 管理员已在服务器配置 SUTUI_SERVER_TOKEN 或 SUTUI_SERVER_TOKENS。"
                    "可访问 http://本机IP:8000/api/health 查看 mcp.reachable 与 mcp.tools_count。\n\n"
                )
            )
            + "回答使用中文，简洁友好。"
            + " 当用户发送新的短消息（如问候、新问题）时，请直接针对该新消息简短回复，不要重复或延续上一条长回复的内容。"
        )
        messages = [{"role": "system", "content": sys_prompt}]
        for m in (payload.history or []):
            if m.role in ("user", "assistant") and (m.content or "").strip():
                content = m.content.strip()
                if len(content) > MAX_HISTORY_MESSAGE_CHARS:
                    content = (
                        content[: MAX_HISTORY_MESSAGE_CHARS // 2].rstrip()
                        + "\n\n...(上条内容已省略，请根据用户新消息直接回复。)"
                    )
                messages.append({"role": m.role, "content": content})
        if len(messages) > MAX_HISTORY + 1:
            messages = [messages[0]] + messages[-MAX_HISTORY:]
        stream_attachment_urls: List[str] = []
        try:
            messages.append(
                {
                    "role": "user",
                    "content": _build_user_content_with_attachments(
                        payload, _request_for_assets, db=db, user_id=current_user.id
                    ),
                }
            )
            stream_attachment_urls = _get_attachment_public_urls(
                payload, _request_for_assets, db=db, user_id=current_user.id
            )
        except HTTPException as e:
            det = e.detail
            error_holder.append(det if isinstance(det, str) else str(det))
        cfg = _resolve_config(resolve_model) if resolve_model else None
        if not error_holder:
            try:
                if cfg:
                    if cfg["provider"] == "anthropic":
                        reply = await _chat_anthropic(
                            messages, cfg, mcp_tools, raw_token,
                            sutui_token=sutui_token,
                            progress_cb=progress_cb,
                            attachment_urls=stream_attachment_urls,
                            db=db, user_id=current_user.id,
                        )
                    else:
                        reply = await _chat_openai(
                            messages, cfg, mcp_tools, raw_token,
                            sutui_token=sutui_token,
                            progress_cb=progress_cb,
                            attachment_urls=stream_attachment_urls,
                            db=db, user_id=current_user.id,
                        )
                    reply_holder.append(reply)
                    _flush_tool_logs(db, current_user.id, payload.session_id, model)
                    _log_turn(
                        db, current_user.id, payload.message, _reply_for_user(reply),
                        payload.session_id, payload.context_id,
                        {"model": model, "mode": "direct", "tools": len(mcp_tools)},
                    )
                    db.commit()
                else:
                    oc_reply = await _try_openclaw(messages, model, raw_token)
                    if oc_reply:
                        reply_holder.append(oc_reply)
                        _flush_tool_logs(db, current_user.id, payload.session_id, model)
                        _log_turn(
                            db, current_user.id, payload.message, _reply_for_user(oc_reply),
                            payload.session_id, payload.context_id,
                            {"model": model, "mode": "openclaw"},
                        )
                        db.commit()
                    else:
                        error_holder.append("LLM 服务暂时不可用")
            except HTTPException as e:
                error_holder.append(e.detail if isinstance(e.detail, str) else str(e.detail))
            except Exception as e:
                logger.exception("chat/stream run_chat error")
                error_holder.append(str(e))
        final_reply = reply_holder[0] if reply_holder else ""
        final_error = error_holder[0] if error_holder else None
        if final_error:
            final_reply = f"错误：{final_error}"
        else:
            final_reply = _reply_for_user(final_reply)
        logger.info("[素材] 对话结束 session_id=%s reply_len=%d error=%s", session_id, len(final_reply or ""), bool(final_error))
        await queue.put({"type": "done", "reply": final_reply, "error": final_error})

    task = asyncio.create_task(run_chat())
    try:
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
                continue
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("type") == "done":
                break
    finally:
        await task


@router.post("/chat/stream", summary="智能对话（流式返回思考/工具进度）")
async def chat_stream_endpoint(
    request: Request,
    payload: ChatRequest,
    raw_token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream SSE events: tool_start, tool_end, then done with reply. Frontend can show progress in chat."""
    return StreamingResponse(
        _chat_stream_events(payload, raw_token, current_user, db, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Chat history ──────────────────────────────────────────────────

@router.get("/chat/history", summary="会话历史")
def list_chat_history(
    context_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(ChatTurnLog).filter(ChatTurnLog.user_id == current_user.id)
    if context_id:
        q = q.filter(ChatTurnLog.context_id == context_id)
    rows = (
        q.order_by(ChatTurnLog.created_at.desc())
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 500))
        .all()
    )
    return [
        {
            "id": r.id,
            "session_id": r.session_id,
            "context_id": r.context_id,
            "user_message": r.user_message,
            "assistant_reply": r.assistant_reply,
            "meta": r.meta,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]


# ── Tool call logs (生产记录) ─────────────────────────────────────

@router.get("/api/tool-logs", summary="MCP 工具调用记录")
def list_tool_logs(
    tool_name: Optional[str] = None,
    success_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(ToolCallLog).filter(ToolCallLog.user_id == current_user.id)
    if tool_name:
        q = q.filter(ToolCallLog.tool_name == tool_name)
    if success_only:
        q = q.filter(ToolCallLog.success.is_(True))
    total = q.count()
    rows = (
        q.order_by(ToolCallLog.created_at.desc())
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "tool_name": r.tool_name,
                "arguments": r.arguments,
                "result_text": r.result_text[:2000] if r.result_text else None,
                "result_urls": r.result_urls.split(",") if r.result_urls else [],
                "success": r.success,
                "latency_ms": r.latency_ms,
                "session_id": r.session_id,
                "model": r.model,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ],
    }


@router.get("/api/tool-logs/stats", summary="工具调用统计")
def tool_log_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from sqlalchemy import Integer as SaInt, func
    rows = (
        db.query(
            ToolCallLog.tool_name,
            func.count(ToolCallLog.id).label("count"),
            func.sum(ToolCallLog.success.is_(True).cast(SaInt)).label("success_count"),
        )
        .filter(ToolCallLog.user_id == current_user.id)
        .group_by(ToolCallLog.tool_name)
        .all()
    )
    return [
        {"tool_name": r.tool_name, "count": r.count, "success_count": r.success_count or 0}
        for r in rows
    ]


# ── 生产记录：仅速推能力调用 + 模型对话，无重复 ─────────────────────────────

def _production_records_merged(
    current_user: User,
    db: Session,
    limit: int = 50,
    offset: int = 0,
):
    """合并 CapabilityCallLog（速推生成）与 ChatTurnLog（模型调用），按时间倒序，无重复。"""
    cap_rows = (
        db.query(CapabilityCallLog)
        .filter(CapabilityCallLog.user_id == current_user.id)
        .order_by(CapabilityCallLog.created_at.desc())
        .limit(150)
        .all()
    )
    turn_rows = (
        db.query(ChatTurnLog)
        .filter(ChatTurnLog.user_id == current_user.id)
        .order_by(ChatTurnLog.created_at.desc())
        .limit(150)
        .all()
    )
    merged = []
    for r in cap_rows:
        merged.append({
            "type": "capability",
            "id": f"c{r.id}",
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "capability_id": r.capability_id,
            "success": r.success,
            "latency_ms": r.latency_ms,
            "error_message": (r.error_message or "")[:500] if r.error_message else None,
            "status": r.status,
        })
    for r in turn_rows:
        merged.append({
            "type": "model",
            "id": f"t{r.id}",
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "user_message": (r.user_message or "")[:300],
            "assistant_reply": (r.assistant_reply or "")[:500],
        })
    merged.sort(key=lambda x: x["created_at"], reverse=True)
    total = len(merged)
    page = merged[offset : offset + limit]
    return {"total": total, "items": page}


@router.get("/api/production/records", summary="生产记录（仅速推能力+模型对话）")
def list_production_records(
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """仅返回：速推能力调用（CapabilityCallLog）、模型对话轮次（ChatTurnLog）。可刷新看进度，无重复。"""
    return _production_records_merged(current_user=current_user, db=db, limit=min(max(limit, 1), 100), offset=max(offset, 0))


@router.post("/api/production/refresh-pending", summary="刷新待处理（兼容）")
def production_refresh_pending(current_user: User = Depends(get_current_user)):
    """兼容旧前端，无实际操作，返回成功。"""
    return {"ok": True}
