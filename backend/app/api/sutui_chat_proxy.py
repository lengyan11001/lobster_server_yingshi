"""鉴权后统一使用服务器赞助/管理端速推 Token 池，转发 OpenAI 兼容 chat/completions 至 api.xskill.ai。"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from decimal import Decimal
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from mcp.sutui_tokens import next_sutui_server_token_with_pool, sutui_token_recon_meta, sutui_token_ref_from_secret

from .auth import brand_mark_for_jwt_claim
from ..core.config import settings
from ..db import SessionLocal, get_db
from ..models import User
from ..services.credit_ledger import append_credit_ledger
from ..services.credits_amount import credits_json_float, quantize_credits, user_balance_decimal
from ..services.sutui_api_audit import clip_openai_chat_completions_json_for_audit, log_xskill_http
from ..services.sutui_pricing import (
    credits_from_chat_usage_when_no_docs_pricing,
    credits_from_direct_api_usage,
    estimate_credits_from_pricing,
    estimate_pre_deduct_credits,
    extract_upstream_billing_snapshot,
    extract_upstream_reported_credits,
    fetch_model_pricing,
)
from .auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

TRACE_HEADER = "X-Lobster-Chat-Trace-Id"

# ---------------------------------------------------------------------------
# Global connection pool — reuses TCP+TLS connections across requests
# ---------------------------------------------------------------------------
_xskill_pool_client: Optional[httpx.AsyncClient] = None


def _get_xskill_client(timeout: float = 120.0) -> httpx.AsyncClient:
    global _xskill_pool_client
    if _xskill_pool_client is None or _xskill_pool_client.is_closed:
        _xskill_pool_client = httpx.AsyncClient(
            timeout=timeout,
            trust_env=True,
            limits=httpx.Limits(
                max_connections=40,
                max_keepalive_connections=10,
                keepalive_expiry=120,
            ),
        )
    return _xskill_pool_client


# ---------------------------------------------------------------------------
# Direct API pool — for models with self-hosted API keys (bypass xskill)
# ---------------------------------------------------------------------------
_direct_pool_clients: Dict[str, httpx.AsyncClient] = {}


def _get_direct_client(provider: str, timeout: float = 30.0) -> httpx.AsyncClient:
    global _direct_pool_clients
    c = _direct_pool_clients.get(provider)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(
            timeout=timeout,
            trust_env=True,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=5,
                keepalive_expiry=120,
            ),
        )
        _direct_pool_clients[provider] = c
    return c


def _get_direct_route(model: str) -> Optional[Dict[str, str]]:
    """If a model has a direct API key configured, return route info; else None."""
    mid = (model or "").strip()
    if mid in ("deepseek-chat", "deepseek-reasoner"):
        key = (getattr(settings, "deepseek_api_key", None) or "").strip()
        if key:
            base = (getattr(settings, "deepseek_api_base", None) or "https://api.deepseek.com").rstrip("/")
            return {"api_base": base, "api_key": key, "provider": "deepseek"}
    return None


# ---------------------------------------------------------------------------
# Circuit breaker — skip models that timeout repeatedly
# ---------------------------------------------------------------------------
_MODEL_TIMEOUT_WINDOW = 300  # 5 min window
_MODEL_TIMEOUT_THRESHOLD = 3  # 3 timeouts in window → trip
_MODEL_COOLDOWN = 120  # skip for 2 min after tripped
_model_timeout_history: Dict[str, List[float]] = {}
_model_tripped_until: Dict[str, float] = {}


def _record_model_timeout(model: str) -> None:
    now = time.monotonic()
    hist = _model_timeout_history.setdefault(model, [])
    hist.append(now)
    cutoff = now - _MODEL_TIMEOUT_WINDOW
    _model_timeout_history[model] = [t for t in hist if t > cutoff]
    if len(_model_timeout_history[model]) >= _MODEL_TIMEOUT_THRESHOLD:
        _model_tripped_until[model] = now + _MODEL_COOLDOWN
        logger.warning("[circuit-breaker] model=%s tripped for %ss after %d timeouts",
                       model, _MODEL_COOLDOWN, len(_model_timeout_history[model]))


def _record_model_success(model: str) -> None:
    _model_timeout_history.pop(model, None)
    _model_tripped_until.pop(model, None)


def _is_model_tripped(model: str) -> bool:
    until = _model_tripped_until.get(model)
    if until is None:
        return False
    if time.monotonic() >= until:
        _model_tripped_until.pop(model, None)
        _model_timeout_history.pop(model, None)
        return False
    return True


_SUTUI_PROVIDER_PREFIXES = ("anthropic/", "openai/", "google/", "deepseek/", "meta/", "mistral/", "cohere/")

# ---------------------------------------------------------------------------
# Request body optimiser — reduce prompt tokens sent to upstream LLM
# ---------------------------------------------------------------------------
_SLIM_MSG_MAX_TURNS = 12          # keep system + last N user/assistant turns
_SLIM_MSG_MAX_CHARS = 800         # per-message content truncation
_SLIM_TOOL_DESC_MAX = 120         # max chars for tool-level description
_SLIM_PROP_DESC_MAX = 40          # max chars for property-level description; 0 to strip all
_BASE64_RE = __import__("re").compile(r"data:[^;]{0,60};base64,[A-Za-z0-9+/=]{200,}")


_TOOLS_BLACKLIST = frozenset({
    "browser", "canvas", "exec", "process",
})


def _filter_local_tools(tools: list) -> list:
    """Remove OpenClaw-local tools that the server-side LLM should not control."""
    if not isinstance(tools, list):
        return tools
    return [
        t for t in tools
        if not isinstance(t, dict)
        or (t.get("function", t) or {}).get("name", "") not in _TOOLS_BLACKLIST
    ]


def _slim_tools(tools: list) -> list:
    """Shorten tool definitions: truncate descriptions, strip verbose property docs."""
    if not isinstance(tools, list):
        return tools
    out = []
    for t in tools:
        if not isinstance(t, dict):
            out.append(t)
            continue
        fn = t.get("function", t)
        if not isinstance(fn, dict):
            out.append(t)
            continue
        fn2 = dict(fn)
        name = fn2.get("name", "")
        is_lobster = isinstance(name, str) and name.startswith("lobster__")
        desc = fn2.get("description", "")
        desc_limit = 500 if is_lobster else _SLIM_TOOL_DESC_MAX
        if isinstance(desc, str) and len(desc) > desc_limit:
            fn2["description"] = desc[:desc_limit].rstrip() + "…"
        params = fn2.get("parameters") or fn2.get("inputSchema")
        if isinstance(params, dict):
            fn2["parameters" if "parameters" in fn2 else "inputSchema"] = _slim_schema(params)
        if "function" in t:
            out.append({**t, "function": fn2})
        else:
            out.append(fn2)
    return out


def _slim_schema(schema: dict) -> dict:
    """Recursively compact a JSON-Schema: shorten / strip property descriptions."""
    if not isinstance(schema, dict):
        return schema
    s = dict(schema)
    props = s.get("properties")
    if isinstance(props, dict):
        new_props = {}
        for k, v in props.items():
            if not isinstance(v, dict):
                new_props[k] = v
                continue
            v2 = dict(v)
            pd = v2.get("description", "")
            if isinstance(pd, str):
                if _SLIM_PROP_DESC_MAX <= 0:
                    v2.pop("description", None)
                elif len(pd) > _SLIM_PROP_DESC_MAX:
                    v2["description"] = pd[:_SLIM_PROP_DESC_MAX].rstrip() + "…"
            if "properties" in v2:
                v2 = _slim_schema(v2)
            new_props[k] = v2
        s["properties"] = new_props
    return s


def _slim_messages(messages: list) -> list:
    """Trim conversation history: keep system msg + last N turns, truncate long content, strip base64."""
    if not isinstance(messages, list) or len(messages) <= 2:
        return messages
    sys_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
    non_sys = [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]
    non_sys = [
        m for m in non_sys
        if not (
            m.get("role") == "assistant"
            and isinstance(m.get("content"), str)
            and m["content"].startswith("错误：")
        )
    ]
    if len(non_sys) > _SLIM_MSG_MAX_TURNS:
        non_sys = non_sys[-_SLIM_MSG_MAX_TURNS:]
    non_sys = _repair_orphan_tool_messages(non_sys)
    result = []
    for m in sys_msgs:
        result.append(_truncate_msg(m))
    for m in non_sys:
        result.append(_truncate_msg(m))
    return result


def _repair_orphan_tool_messages(messages: list) -> list:
    """Remove orphaned tool messages whose preceding tool_calls assistant message was truncated.

    DeepSeek (and OpenAI) require every message with role='tool' to follow
    an assistant message that contains a matching tool_calls entry.  After
    truncation this invariant can break.  We also strip assistant messages
    whose tool_calls all lost their tool responses (to avoid dangling calls).
    """
    if not messages:
        return messages

    tool_call_ids_from_assistants: set = set()
    tool_call_id_to_tool_idx: dict = {}

    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").strip().lower()
        if role == "assistant":
            tcs = m.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    tc_id = (tc.get("id") or "") if isinstance(tc, dict) else ""
                    if tc_id:
                        tool_call_ids_from_assistants.add(tc_id)
        elif role == "tool":
            tc_id = (m.get("tool_call_id") or "").strip()
            if tc_id:
                tool_call_id_to_tool_idx[tc_id] = i

    orphan_indices: set = set()
    for tc_id, idx in tool_call_id_to_tool_idx.items():
        if tc_id not in tool_call_ids_from_assistants:
            orphan_indices.add(idx)

    if not orphan_indices:
        return messages

    repaired = [m for i, m in enumerate(messages) if i not in orphan_indices]
    logger.info(
        "[body-slim] removed %d orphan tool message(s) to fix conversation structure",
        len(orphan_indices),
    )
    return repaired


def _truncate_msg(m: dict) -> dict:
    if not isinstance(m, dict):
        return m
    c = m.get("content")
    if isinstance(c, str):
        c = _BASE64_RE.sub("[image]", c)
        if len(c) > _SLIM_MSG_MAX_CHARS:
            c = c[:_SLIM_MSG_MAX_CHARS] + "…(truncated)"
        if c != m.get("content"):
            return {**m, "content": c}
    elif isinstance(c, list):
        new_parts = []
        for part in c:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if isinstance(url, str) and url.startswith("data:"):
                    new_parts.append({"type": "text", "text": "[image attached]"})
                    continue
            new_parts.append(part)
        if new_parts != c:
            return {**m, "content": new_parts}
    return m


_LOBSTER_SYSTEM_HINT = (
    "【龙虾工具使用规则】"
    "1. 生成图片：用 lobster__invoke_capability，capability_id=\"image.generate\"，"
    "用户未指定模型时 payload.model 填 \"fal-ai/flux-2/flash\"。"
    "2. 生成视频：用 lobster__invoke_capability，capability_id=\"video.generate\"，"
    "用户未指定模型时 payload.model 填 \"sora2\"，未指定时长时 duration=4。"
    "3. 如果调用失败（积分不足、模型错误等），直接将错误信息告知用户，不要尝试用其他方式（搜索、网页等）来替代。"
    "4. 用户说TVC/带货视频时用 capability_id=\"comfly.daihuo.pipeline\"。"
    "5. 生成后如需保存素材用 lobster__save_asset；发布内容用 lobster__publish_content。"
    "6. 用户问有哪些技能、能力、功能时，只需调 list_capabilities 一次即可总结回复，"
    "不要额外调 manage_skills(list_installed/list_store/search_online)。"
    "7. 工具调用要精简高效，拿到足够信息后立即用文本回复，禁止冗余重复调用。"
    "8. 【写文章/写文案 — 先文字后工具】用户说「写一篇 XX 字的文章」「帮我写 XX 文案」「写一段 XX」等纯文字创作任务时，"
    "**第一步必须**直接用文字写出正文回复用户，**禁止**先调 image.generate / video.generate 生成配图/封面"
    "（除非用户原话中明确说「配图」「封面」「图文」「带图」）。"
    "用户在写文章请求里追加「发去头条/公众号/发布」时，正确流程是：① 直接写出正文给用户看 → ② 询问"
    "「是否直接发布纯文字版（无封面），还是要我加配图」 → ③ 按用户回答调 publish_content（纯文字时设 toutiao_graphic_no_cover:true）。"
    "头条号支持纯文字发布，**禁止**报「已完成」但实际只调了 image.generate 而没调 publish_content。"
)


def _inject_lobster_system_hint(body: dict) -> None:
    """Append lobster usage rules to the first system message, or prepend a new one."""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return
    tools = body.get("tools")
    if not isinstance(tools, list):
        return
    has_lobster_tool = any(
        isinstance(t, dict) and (t.get("function", t) or {}).get("name", "").startswith("lobster__")
        for t in tools
    )
    if not has_lobster_tool:
        return
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system":
            existing = m.get("content") or ""
            if "龙虾工具使用规则" not in existing:
                m["content"] = existing + "\n\n" + _LOBSTER_SYSTEM_HINT
            return
    msgs.insert(0, {"role": "system", "content": _LOBSTER_SYSTEM_HINT})


def _optimize_request_body(body: dict) -> int:
    """Optimise body in-place before forwarding. Returns estimated token savings."""
    import copy
    orig_len = len(json.dumps(body, ensure_ascii=False, default=str))

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        tools = _filter_local_tools(tools)
        body["tools"] = _slim_tools(tools)

    _inject_lobster_system_hint(body)

    msgs = body.get("messages")
    if isinstance(msgs, list):
        body["messages"] = _slim_messages(msgs)

    new_len = len(json.dumps(body, ensure_ascii=False, default=str))
    saved_chars = orig_len - new_len
    if saved_chars > 500:
        logger.info(
            "[body-slim] payload %d→%d chars (saved %d, ~%d tokens)",
            orig_len, new_len, saved_chars, saved_chars // 3,
        )
    return saved_chars


def _messages_already_ran_search_models(messages: Any) -> bool:
    """
    防止同一轮对话里反复调用 sutui.search_models。

    只在“已经真正执行过一次 search_models（出现 tool 结果/工具回传）”后才认为已完成，
    这样不会误伤首次查询（否则会导致模型拿不到 tools，只能用文本伪造一段 JSON 工具调用）。
    """
    if not isinstance(messages, list) or not messages:
        return False

    needles = ("sutui.search_models", "capability_id\": \"sutui.search_models\"")
    for m in messages[-24:]:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").strip().lower()

        # OpenAI: tool role message content often contains tool result JSON/text
        if role == "tool":
            c = m.get("content")
            if isinstance(c, str) and any(n in c for n in needles):
                return True
            # some gateways use list parts
            if isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and isinstance(part.get("text"), str) and any(
                        n in part["text"] for n in needles
                    ):
                        return True

        # Also accept assistant message that includes tool_calls array (structured)
        if role == "assistant":
            tcs = m.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function")
                    if not isinstance(fn, dict):
                        continue
                    if (fn.get("name") or "").strip() != "invoke_capability":
                        continue
                    args = fn.get("arguments")
                    if isinstance(args, str) and "sutui.search_models" in args:
                        return True

    return False


def _enforce_single_search_models_tool_call(body: Dict[str, Any], trace_id: str) -> None:
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return
    msgs = body.get("messages")
    if not _messages_already_ran_search_models(msgs):
        return
    # 已查过 search_models：从工具的 capability_id enum 中移除 search_models，保留其他工具。
    _search_needles = {"sutui.search_models"}
    modified = False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if not isinstance(fn, dict):
            continue
        params = fn.get("parameters")
        if not isinstance(params, dict):
            continue
        props = params.get("properties")
        if not isinstance(props, dict):
            continue
        cap_prop = props.get("capability_id")
        if isinstance(cap_prop, dict) and isinstance(cap_prop.get("enum"), list):
            original = cap_prop["enum"]
            filtered = [e for e in original if e not in _search_needles]
            if len(filtered) < len(original):
                cap_prop["enum"] = filtered
                modified = True
    if modified:
        logger.info(
            "[chat_trace] trace_id=%s enforce_single_search_models: removed search_models from capability enum (tools preserved)",
            trace_id,
        )
    else:
        logger.info(
            "[chat_trace] trace_id=%s enforce_single_search_models: no enum to filter, keeping tools as-is",
            trace_id,
        )


def _strip_provider_prefix(mid: str) -> str:
    """速推 model id 不带 provider 前缀（如 claude-opus-4-6 而非 anthropic/claude-opus-4-6）。"""
    for pfx in _SUTUI_PROVIDER_PREFIXES:
        if mid.startswith(pfx):
            return mid[len(pfx):]
    return mid


def _remap_sutui_chat_model(body: Dict[str, Any]) -> None:
    """将客户端传来的 model 映射为速推分销商侧实际有通道的 id（就地修改 body）。

    1. 自动剥离 provider 前缀（anthropic/、openai/ 等）
    2. 环境变量 SUTUI_CHAT_MODEL_MAP_JSON：JSON 对象，键为入站 model 字符串，值为转发到 xskill 的 model。
    """
    mid = (body.get("model") or "").strip()
    if not mid:
        return
    stripped = _strip_provider_prefix(mid)
    if stripped != mid:
        logger.info("[sutui-chat] 自动剥离 provider 前缀: %s -> %s", mid, stripped)
        body["model"] = stripped
        mid = stripped
    raw = (os.environ.get("SUTUI_CHAT_MODEL_MAP_JSON") or "").strip()
    if not raw:
        return
    try:
        m = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[sutui-chat] SUTUI_CHAT_MODEL_MAP_JSON 不是合法 JSON，已忽略")
        return
    if not isinstance(m, dict):
        return
    to_id = m.get(mid)
    if isinstance(to_id, str) and to_id.strip():
        logger.info("[sutui-chat] SUTUI_CHAT_MODEL_MAP_JSON 映射 model: %s -> %s", mid, to_id.strip())
        body["model"] = to_id.strip()


# 日志中单条响应最大字符（避免 choices 正文撑爆日志）
_SUTUI_CHAT_LOG_BODY_MAX = 24_000


def _sutui_chat_upstream_body_for_log(data: Optional[Dict[str, Any]]) -> str:
    """保留 usage、id、model、计费相关嵌套字段；choices 只保留索引/角色，不打印正文。"""
    if not isinstance(data, dict):
        return ""
    slim: Dict[str, Any] = {}
    for key in ("id", "object", "created", "model", "system_fingerprint", "usage", "service_tier"):
        if key in data:
            slim[key] = data[key]
    ch = data.get("choices")
    if isinstance(ch, list):
        slim["choices"] = []
        for c in ch[:8]:
            if not isinstance(c, dict):
                continue
            entry: Dict[str, Any] = {"index": c.get("index"), "finish_reason": c.get("finish_reason")}
            msg = c.get("message")
            if isinstance(msg, dict):
                entry["message"] = {
                    "role": msg.get("role"),
                    "content_len": len(msg.get("content") or "") if isinstance(msg.get("content"), str) else None,
                }
            slim["choices"].append(entry)
    # 其余顶层键（常为速推扩展：计费、扩展字段）
    for k, v in data.items():
        if k in slim or k == "choices":
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
                "x-",
                "sutui",
            )
        ):
            slim[k] = v
    try:
        raw = json.dumps(slim, ensure_ascii=False, default=str)
    except Exception:
        raw = str(slim)[:2000]
    if len(raw) > _SUTUI_CHAT_LOG_BODY_MAX:
        return raw[:_SUTUI_CHAT_LOG_BODY_MAX] + f"... [截断，原约 {len(raw)} 字符]"
    return raw


def _api_base() -> str:
    return (getattr(settings, "sutui_api_base", None) or "https://api.xskill.ai").rstrip("/")


# ---------------------------------------------------------------------------
# xskill v3 (OpenRouter) — new endpoint with provider-prefixed model IDs
# ---------------------------------------------------------------------------
_XSKILL_V3_MODEL_MAP: Dict[str, str] = {
    "deepseek-chat": "deepseek/deepseek-v3.2",
    "deepseek-reasoner": "deepseek/deepseek-v3.2-speciale",
}
_XSKILL_V3_CREDITS_PER_USD = 400


def _get_v3_route(model: str, token: str) -> Optional[Dict[str, Any]]:
    """Return v3 attempt dict if model has a v3 mapping, else None."""
    v3_model = _XSKILL_V3_MODEL_MAP.get(model)
    if not v3_model:
        return None
    return {
        "model": v3_model,
        "api_base": _api_base(),
        "api_key": token,
        "provider": "xskill-v3",
        "endpoint_prefix": "/v3",
        "is_direct": False,
        "v1_model": model,
    }


def _upstream_chat_error_dict(data: Any) -> Optional[Dict[str, Any]]:
    """从速推 chat/completions 错误 JSON 中取出 error 对象（兼容 detail.error）。"""
    if not isinstance(data, dict):
        return None
    e = data.get("error")
    if isinstance(e, dict):
        return e
    d = data.get("detail")
    if isinstance(d, dict):
        e2 = d.get("error")
        if isinstance(e2, dict):
            return e2
    return None


def _xskill_upstream_pool_quota_error(data: Any) -> bool:
    """
    速推返回的「Token 池美元预扣/余额」类错误：与龙虾用户积分无关，不应让用户以为是自己积分不够。
    """
    if not isinstance(data, dict):
        return False
    err = _upstream_chat_error_dict(data)
    if not isinstance(err, dict):
        return False
    code = str(err.get("code") or "").strip().lower()
    typ = str(err.get("type") or "").strip().lower()
    raw_msg = str(err.get("message") or "")
    msg = raw_msg.strip().lower()
    return (
        code in ("insufficient_balance", "insufficient_user_quota")
        or typ == "billing_error"
        or (typ.replace("_", "") == "rixapierror" and ("insufficient" in code or "预扣费" in raw_msg))
        or "预扣费" in raw_msg
        or "insufficient" in msg
    )


def _parse_sutui_chat_fallback_chain_env() -> List[str]:
    """主→备模型顺序：默认 deepseek-chat → claude-opus-4-6；可用 SUTUI_CHAT_MODEL_FALLBACK_CHAIN_JSON 覆盖。"""
    raw = (os.environ.get("SUTUI_CHAT_MODEL_FALLBACK_CHAIN_JSON") or "").strip()
    default = ["deepseek-chat", "claude-opus-4-6"]
    if not raw:
        return default
    try:
        arr = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[sutui-chat] SUTUI_CHAT_MODEL_FALLBACK_CHAIN_JSON 不是合法 JSON，已用默认链")
        return default
    if not isinstance(arr, list) or not arr:
        return default
    out = [str(x).strip() for x in arr if str(x).strip()]
    return out if out else default


def _remap_model_id_for_sutui(mid: str) -> str:
    """对单个 model id 应用 SUTUI_CHAT_MODEL_MAP_JSON（与 body 一致）。"""
    b: Dict[str, Any] = {"model": (mid or "").strip()}
    if not b["model"]:
        return ""
    _remap_sutui_chat_model(b)
    return (b.get("model") or "").strip()


def _sutui_chat_model_candidates(initial_model: str, *, has_tools: bool = False) -> List[str]:
    """
    对话编排：优先用入站（已 remap）的 model，再按 fallback chain 尝试其它通道，去重。
    熔断的模型会被跳过（但保留至少一个候选）。
    """
    seen: set[str] = set()
    out: List[str] = []
    init = (initial_model or "").strip()

    if init and init not in seen:
        seen.add(init)
        out.append(init)
    for fb in _parse_sutui_chat_fallback_chain_env():
        m = _remap_model_id_for_sutui(fb)
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    if not out:
        return [init] if init else []
    filtered = [m for m in out if not _is_model_tripped(m)]
    if not filtered:
        logger.info("[circuit-breaker] all candidates tripped, using first: %s", out[0])
        return [out[0]]
    if len(filtered) < len(out):
        skipped = set(out) - set(filtered)
        logger.info("[circuit-breaker] skipping tripped models: %s", skipped)
    return filtered


def _openai_nonstream_completion_usable(data: Any, http_status: int) -> bool:
    """非流式：须为 200 且含非空 choices，且不应为上游业务/池错误 JSON。"""
    if http_status != 200:
        return False
    if not isinstance(data, dict):
        return False
    top_err = data.get("error")
    if isinstance(top_err, dict) and top_err.get("message"):
        return False
    if isinstance(top_err, str) and top_err.strip():
        return False
    e = _upstream_chat_error_dict(data)
    if isinstance(e, dict) and e.get("message"):
        return False
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) < 1:
        return False
    return True


_FAKE_TOOL_CALL_RE = __import__("re").compile(
    r"tool\u2581call|<\|tool|<\uff5cDSML\uff5c|```json\s*\{[^}]*capability|function<\u2581",
)


def _response_has_fake_tool_text(data: Any) -> bool:
    """Detect deepseek-style fake tool calls embedded in text content."""
    if not isinstance(data, dict):
        return False
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    msg = (choices[0] if isinstance(choices[0], dict) else {}).get("message", {})
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str) and _FAKE_TOOL_CALL_RE.search(content):
        return True
    return False


_DSML_BLOCK_RE = __import__("re").compile(
    r"<\uff5cDSML\uff5c[\s\S]*?(?:</\uff5cDSML\uff5c>|$)",
)


def _strip_fake_tool_text_from_response(data: Any) -> bool:
    """Strip DSML / fake tool call markup from content in-place. Returns True if cleaned."""
    if not isinstance(data, dict):
        return False
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    msg = (choices[0] if isinstance(choices[0], dict) else {}).get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if not isinstance(content, str):
        return False
    if not _FAKE_TOOL_CALL_RE.search(content):
        return False
    cleaned = _DSML_BLOCK_RE.sub("", content).strip()
    if not cleaned:
        cleaned = "好的，我来为您总结一下已获取的信息。"
    if cleaned != content:
        msg["content"] = cleaned
        logger.warning("[dsml-clean] stripped fake tool markup from response content (%d→%d chars)",
                       len(content), len(cleaned))
        return True
    return False


_MAX_TOOL_CALL_ROUNDS = 4


def _enforce_max_tool_call_rounds(body: Dict[str, Any], trace_id: str) -> bool:
    """If conversation already has >= _MAX_TOOL_CALL_ROUNDS tool call round-trips,
    remove tools to force a text-only response. Returns True if tools were stripped."""
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return False
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return False
    rounds = 0
    for m in msgs:
        if isinstance(m, dict) and (m.get("role") or "").strip().lower() == "tool":
            rounds += 1
    if rounds < _MAX_TOOL_CALL_ROUNDS:
        return False
    body.pop("tools", None)
    body.pop("tool_choice", None)
    logger.warning(
        "[chat_trace] trace_id=%s enforce_max_tool_rounds: %d tool rounds detected (max=%d), "
        "stripped tools to force text response",
        trace_id, rounds, _MAX_TOOL_CALL_ROUNDS,
    )
    return True


def _openai_completion_missing_tool_calls(data: Any, request_body: Dict[str, Any]) -> bool:
    """请求中传了 tools 且 tool_choice 非 none，但响应不含 tool_calls——模型未遵从 tool 指令，值得换模型重试。
    包括检测 deepseek 在文本中伪造工具调用标记的情况。"""
    if not isinstance(request_body, dict):
        return False
    tools = request_body.get("tools")
    if not isinstance(tools, list) or not tools:
        return False
    tc = (request_body.get("tool_choice") or "auto")
    if isinstance(tc, str) and tc.strip().lower() == "none":
        return False
    if not isinstance(data, dict):
        return False
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) < 1:
        return False
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return False
    if _response_has_fake_tool_text(data):
        return True
    tcs = msg.get("tool_calls")
    if isinstance(tcs, list) and len(tcs) > 0:
        return False
    return True


def _sutui_chat_abort_model_fallback(http_status: int, data: Any) -> bool:
    """此类结果换模型无意义，直接结束尝试。"""
    if http_status == 401:
        return True
    # xskill chat 上游 402 多为托管池预扣/余额问题，换模型通常仍走同一 Token
    if http_status == 402:
        return True
    if isinstance(data, dict) and _xskill_upstream_pool_quota_error(data):
        return True
    return False


def _stream_upstream_error_sse_bytes(resp_status: int, txt: str) -> bytes:
    """流式连接在收到 HTTP>=400 且无 body 流时，向下游补一条 SSE error（与非流式语义对齐）。"""
    if resp_status == 402:
        try:
            parsed = json.loads(txt) if txt.strip().startswith("{") else {}
        except Exception:
            parsed = {}
        norm = _normalize_upstream_xskill_pool_errors_for_client(parsed if isinstance(parsed, dict) else {})
        if isinstance(norm, dict) and norm.get("error"):
            err = json.dumps(norm, ensure_ascii=False)
        else:
            err = json.dumps(
                {
                    "error": {
                        "message": (
                            "速推服务端账户余额不足（流式上游返回 402）。"
                            "需管理员在速推控制台为服务器 Token 池对应账户充值而你个人龙虾积分若充足则非你欠费。"
                        ),
                        "type": "billing_error",
                        "code": "upstream_insufficient_balance",
                    }
                },
                ensure_ascii=False,
            )
    elif resp_status == 403:
        try:
            parsed403 = json.loads(txt) if txt.strip().startswith("{") else {}
        except Exception:
            parsed403 = {}
        pd = parsed403 if isinstance(parsed403, dict) else {}
        if _xskill_upstream_pool_quota_error(pd):
            norm403 = _normalize_upstream_xskill_pool_errors_for_client(pd)
            err = json.dumps(norm403 if isinstance(norm403, dict) else {"error": norm403}, ensure_ascii=False)
        else:
            err = json.dumps({"error": {"message": txt[:2000], "status": 403}}, ensure_ascii=False)
    else:
        err = json.dumps({"error": {"message": txt[:2000], "status": resp_status}}, ensure_ascii=False)
    return f"data: {err}\n\n".encode("utf-8")


def _normalize_upstream_xskill_pool_errors_for_client(data: Any) -> Any:
    """
    将 xskill Token 池侧余额/预扣美元失败，替换为明确中文。
    LLM 对话事前不按 docs 定价表拦截；此处仅为上游 Token 池故障时的对外说明。
    """
    if not isinstance(data, dict) or not _xskill_upstream_pool_quota_error(data):
        return data
    return {
        "error": {
            "message": (
                "线路暂时不可用：速推（xskill）托管 Token 池在对方侧额度异常，需管理员在速推控制台"
                "为该池充值或更换密钥。你在龙虾的积分若仍充足，并非你个人欠费；请稍后重试或联系客服。"
            ),
            "type": "billing_error",
            "code": "upstream_insufficient_balance",
        }
    }


def _should_deduct_credits() -> bool:
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    return edition == "online" and getattr(settings, "lobster_independent_auth", True)


def _rough_prompt_tokens_from_messages(messages: Any) -> int:
    """粗估 prompt token 数，仅用于预检（略高估，减少「余额够预检但事后不够扣」）。"""
    if not isinstance(messages, list):
        return 512
    total_chars = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total_chars += len(part["text"])
    # 中英混排：偏保守按约每 3 字符 1 token
    return max(32, int(total_chars / 3) + 32)


def _completion_max_for_estimate(body: Dict[str, Any]) -> int:
    """从请求中取最大生成长度；未指定时用中等默认值，避免用满上下文上限误伤正常用户。"""
    mt = body.get("max_tokens") if body.get("max_tokens") is not None else body.get("max_completion_tokens")
    if mt is None:
        return 2048
    try:
        v = int(mt)
    except (TypeError, ValueError):
        return 2048
    return min(max(v, 1), 128_000)


def _chat_balance_precheck_params(body: Dict[str, Any]) -> Dict[str, Any]:
    pt = _rough_prompt_tokens_from_messages(body.get("messages"))
    ct = _completion_max_for_estimate(body)
    return {"prompt_tokens": pt, "completion_tokens": ct}


def _total_tokens_in_usage(usage: Optional[dict]) -> int:
    """是否具备可用的 token 计数（用于判断能否依赖上游 usage 计费）。"""
    if not usage or not isinstance(usage, dict):
        return 0
    tt = usage.get("total_tokens")
    if tt is not None:
        try:
            t = int(tt)
            if t > 0:
                return t
        except (TypeError, ValueError):
            pass
    try:
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        return pt + ct
    except (TypeError, ValueError):
        return 0


def _ensure_chat_usage_for_billing(body: Dict[str, Any], usage: Optional[dict]) -> dict:
    """
    上游 OpenAI 兼容实现常省略 usage（或非流式仅返回 choices）；docs 又无定价时会导致事后 0 扣费。
    与流式分支一致：缺省则用 messages/max_tokens 粗估，至少按 SUTUI_CHAT_FALLBACK_CREDITS_PER_1K 折算。
    """
    if _total_tokens_in_usage(usage) > 0:
        return usage  # type: ignore[return-value]
    sp = _chat_balance_precheck_params(body)
    logger.info(
        "[sutui-chat] billing_usage_fallback pt=%s ct=%s (upstream usage missing or all zero)",
        sp["prompt_tokens"],
        sp["completion_tokens"],
    )
    return {
        "prompt_tokens": sp["prompt_tokens"],
        "completion_tokens": sp["completion_tokens"],
    }


def _require_balance_before_upstream_chat(
    db: Session,
    current_user: User,
    model_id: str,
    body: Dict[str, Any],
) -> None:
    """
    LLM 对话：**不按**速推 docs 定价表预拦（与素材生成不同）；放行后按上游返回的 usage/价字段扣费。
    仅当开启用户扣费且余额≤0 时 402，避免无意义打速推。
    """
    if not _should_deduct_credits():
        return
    db.refresh(current_user)
    bal = user_balance_decimal(current_user)
    if bal <= 0:
        raise HTTPException(
            status_code=402,
            detail="积分不足：当前余额为 0，请充值后再使用智能对话。",
        )


def _credits_for_sutui_chat(
    model: str,
    usage: Optional[dict],
    response_body: Optional[Dict[str, Any]] = None,
    *,
    is_direct_api: bool = False,
) -> Tuple[Decimal, str]:
    """LLM 实扣：v3 cost → 直连官方定价 → 上游显式价字段 → docs 定价+usage → usage×fallback。"""
    if usage and isinstance(usage, dict):
        v3_cost = usage.get("cost")
        if isinstance(v3_cost, (int, float)) and v3_cost > 0:
            v3_credits = quantize_credits(Decimal(str(v3_cost)) * _XSKILL_V3_CREDITS_PER_USD)
            if v3_credits > 0:
                return v3_credits, "v3_cost_field"

    if is_direct_api and usage and isinstance(usage, dict):
        direct_credits = credits_from_direct_api_usage(model, usage)
        if direct_credits > 0:
            return direct_credits, "direct_official_pricing"

    if response_body and isinstance(response_body, dict):
        reported = extract_upstream_reported_credits(response_body)
        if reported > 0:
            return quantize_credits(reported), "upstream价字段优先"

    # 注意：fallback（SUTUI_CHAT_FALLBACK_CREDITS_PER_1K 常为 1）必须在 docs 定价之后，
    # 否则「每千 token 1 积分」会先命中（如 24k token→25），永远轮不到 token_based 真实单价（往往≈0.0x/千）。
    pricing = fetch_model_pricing(model)
    params: Dict[str, Any] = {}
    if usage and isinstance(usage, dict):
        params["prompt_tokens"] = usage.get("prompt_tokens", 0)
        params["completion_tokens"] = usage.get("completion_tokens", 0)
    if pricing:
        est = estimate_credits_from_pricing(pricing, params)
        if est > 0:
            return quantize_credits(est), "docs定价+usage"
        est2, err = estimate_pre_deduct_credits(model, None)
        if not err and est2 > 0:
            return quantize_credits(est2), "docs定价(默认参)"
        if usage and isinstance(usage, dict) and _total_tokens_in_usage(usage) > 0:
            fb2 = credits_from_chat_usage_when_no_docs_pricing(usage, model)
            if fb2 > 0:
                return fb2, "usage折算(docs未算出)"
        logger.warning("[sutui-chat] 有 pricing 结构但仍无法扣费 model=%s usage=%s", model, usage)
        return Decimal(0), "未扣费"

    if usage and isinstance(usage, dict) and _total_tokens_in_usage(usage) > 0:
        fb3 = credits_from_chat_usage_when_no_docs_pricing(usage, model)
        if fb3 > 0:
            return fb3, "usage折算(无docs定价)"
    logger.warning(
        "[sutui-chat] 无定价且 usage 无法折算（可调高 SUTUI_CHAT_FALLBACK_CREDITS_PER_1K 或配置 SUTUI_CHAT_MODEL_MAP） model=%s usage=%s",
        model,
        usage,
    )
    return Decimal(0), "未扣费"


def _apply_chat_deduct(
    db: Session,
    current_user: User,
    model: str,
    usage: Optional[dict],
    response_body: Optional[Dict[str, Any]] = None,
    *,
    billing_recon: Optional[Dict[str, Any]] = None,
    trace_id: Optional[str] = None,
    is_direct_api: bool = False,
) -> None:
    tid = trace_id or "-"
    if not _should_deduct_credits():
        logger.info(
            "[chat_trace] trace_id=%s path=sutui_chat_deduct result=skipped reason=no_user_deduct "
            "edition=%s independent_auth=%s",
            tid,
            getattr(settings, "lobster_edition", None),
            getattr(settings, "lobster_independent_auth", True),
        )
        return
    reported_raw = None
    if response_body and isinstance(response_body, dict):
        reported_raw = extract_upstream_reported_credits(response_body)
    credits, billing_src = _credits_for_sutui_chat(model, usage, response_body, is_direct_api=is_direct_api)
    snap = extract_upstream_billing_snapshot(response_body if isinstance(response_body, dict) else None)
    try:
        snap_json = json.dumps(snap, ensure_ascii=False, default=str)
    except Exception:
        snap_json = str(snap)[:2000]
    logger.info("[sutui-chat] 上游扣费原始结构=%s", snap_json)
    logger.info(
        "[sutui-chat] 计费明细 user_id=%s model=%s 扣费来源=%s 最终扣积分=%s extract_upstream_reported=%s usage=%s 上游响应(节选)=%s",
        current_user.id,
        model,
        billing_src,
        credits,
        reported_raw,
        usage,
        _sutui_chat_upstream_body_for_log(response_body if isinstance(response_body, dict) else None),
    )
    if credits <= 0:
        logger.info(
            "[chat_trace] trace_id=%s path=sutui_chat_deduct result=skipped reason=credits_zero "
            "user_id=%s model=%s billing_src=%s computed_credits=%s usage=%s",
            tid,
            current_user.id,
            model,
            billing_src,
            credits,
            usage,
        )
        return
    db.refresh(current_user)
    bal = user_balance_decimal(current_user)
    if bal < credits:
        logger.error(
            "[sutui-chat] 扣积分失败（余额不足），上游已成功返回，不向客户端透传正文 trace_id=%s user_id=%s model=%s need=%s have=%s",
            tid,
            current_user.id,
            model,
            credits,
            bal,
        )
        raise HTTPException(
            status_code=402,
            detail=(
                f"积分不足：本次应答需扣 {credits} 积分，当前余额 {bal}。请充值后重试。"
            ),
        )
    current_user.credits = bal - credits
    bal_after = quantize_credits(current_user.credits)
    meta_chat = {
        "model": model,
        "usage": usage,
        "deduct_credits": credits_json_float(credits),
        "billing_src": billing_src,
    }
    if billing_recon:
        meta_chat = {**meta_chat, **billing_recon}
    append_credit_ledger(
        db,
        current_user.id,
        -credits,
        "sutui_chat",
        bal_after,
        description=f"速推 LLM 对话扣费 model={model}",
        ref_type="sutui_chat",
        meta=meta_chat,
    )
    db.commit()
    logger.info(
        "[chat_trace] trace_id=%s path=sutui_chat_deduct result=ok ledger=sutui_chat user_id=%s model=%s credits=%s balance_after=%s",
        tid,
        current_user.id,
        model,
        credits,
        bal_after,
    )
    logger.info("[sutui-chat] 已扣积分 trace_id=%s user_id=%s model=%s credits=%s", tid, current_user.id, model, credits)


@router.post("/api/sutui-chat/completions", summary="速推 LLM 对话代理（需登录）")
async def sutui_chat_completions(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体须为 JSON")

    trace_id = (
        (request.headers.get(TRACE_HEADER) or request.headers.get(TRACE_HEADER.lower()) or "").strip()
        or uuid.uuid4().hex
    )
    logger.info(
        "[chat_trace] trace_id=%s path=sutui_chat_completions enter user_id=%s stream=%s model_in=%s",
        trace_id,
        current_user.id,
        bool(body.get("stream")),
        (body.get("model") or "-"),
    )

    _remap_sutui_chat_model(body)
    _optimize_request_body(body)
    _enforce_single_search_models_tool_call(body, trace_id)
    _enforce_max_tool_call_rounds(body, trace_id)

    bm = brand_mark_for_jwt_claim(getattr(current_user, "brand_mark", None))
    if bm not in ("bihuo", "yingshi"):
        raise HTTPException(
            status_code=403,
            detail="当前账号未绑定必火/影视品牌，无法使用速推对话；无通用兜底。请使用对应品牌客户端注册或联系管理员补全品牌后重新登录。",
        )
    token, sutui_pool = await next_sutui_server_token_with_pool(brand_mark=bm)
    if not token:
        raise HTTPException(
            status_code=503,
            detail="服务器未配置当前品牌对应的速推 Token（SUTUI_SERVER_TOKENS_BIHUO 或 SUTUI_SERVER_TOKENS_YINGSHI）",
        )
    chat_billing_recon = sutui_token_recon_meta(token, sutui_pool)

    stream = bool(body.get("stream"))
    url = f"{_api_base()}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    model_id = (body.get("model") or "").strip()
    _req_has_tools = bool(body.get("tools")) and body.get("tool_choice") != "none"
    model_candidates = _sutui_chat_model_candidates(model_id, has_tools=_req_has_tools)
    _tok = (token or "").strip()
    _tok_ref = sutui_token_ref_from_secret(_tok) or "-"
    _tok_tail = _tok[-6:] if len(_tok) > 6 else "***"
    logger.info(
        "[sutui-chat] 转发速推请求：POST %s | user_id=%s brand_mark=%s sutui_pool=%s | "
        "sutui_token_ref=%s sutui_token_tail=%s | model=%s stream=%s trace_id=%s candidates=%s",
        url,
        current_user.id,
        bm,
        sutui_pool or "-",
        _tok_ref,
        _tok_tail,
        model_id or "-",
        stream,
        trace_id,
        model_candidates,
    )
    logger.info(
        "[chat_trace] trace_id=%s path=sutui_chat_completions forward brand=%s model_after_remap=%s sutui_pool=%s model_candidates=%s",
        trace_id,
        bm,
        model_id or "-",
        sutui_pool or "-",
        model_candidates,
    )

    _require_balance_before_upstream_chat(db, current_user, model_id, body)

    user_bal_str = "-"
    if _should_deduct_credits():
        db.refresh(current_user)
        user_bal_str = str(user_balance_decimal(current_user))
    out_req_for_audit = clip_openai_chat_completions_json_for_audit(body)

    # ── Build attempts list: direct → xskill-v3 → xskill-v1 ──
    _DIRECT_TIMEOUT = 180.0
    _XSKILL_TIMEOUT = 180.0
    attempts: List[Dict[str, Any]] = []
    for mid in model_candidates:
        dr = _get_direct_route(mid)
        if dr:
            attempts.append({
                "model": mid, "api_base": dr["api_base"], "api_key": dr["api_key"],
                "provider": "direct:" + dr["provider"], "timeout": _DIRECT_TIMEOUT, "is_direct": True,
            })
        v3 = _get_v3_route(mid, token)
        if v3:
            attempts.append({
                "model": v3["model"], "api_base": v3["api_base"], "api_key": v3["api_key"],
                "provider": v3["provider"], "timeout": _XSKILL_TIMEOUT, "is_direct": False,
                "endpoint_prefix": v3["endpoint_prefix"], "v1_model": mid,
            })
        attempts.append({
            "model": mid, "api_base": _api_base(), "api_key": token,
            "provider": "xskill", "timeout": _XSKILL_TIMEOUT, "is_direct": False,
        })
    logger.info(
        "[chat_trace] trace_id=%s attempts=%s",
        trace_id,
        [(a["model"], a["provider"], a["timeout"]) for a in attempts],
    )

    if not stream:
        r: Optional[httpx.Response] = None
        data: Any = None
        winning_model = model_id
        winning_is_direct = False
        winning_provider = ""
        last_connect_error: Optional[Exception] = None
        last_timeout_error: Optional[Exception] = None

        for attempt_idx, att in enumerate(attempts):
            mid_try = att["model"]
            _epfx = att.get("endpoint_prefix", "/v1")
            att_url = f"{att['api_base']}{_epfx}/chat/completions"
            att_headers = {
                "Authorization": f"Bearer {att['api_key']}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            att_is_direct = att["is_direct"]
            _saved_model = body.get("model")
            body["model"] = mid_try

            if att_is_direct:
                client = _get_direct_client(att["provider"], timeout=att["timeout"])
            else:
                client = _get_xskill_client(timeout=att["timeout"])

            try:
                r = await client.post(att_url, json=body, headers=att_headers)
            except httpx.ConnectError as e:
                last_connect_error = e
                logger.warning(
                    "[sutui-chat] 上游连接失败 attempt=%s model=%s provider=%s trace_id=%s err=%s",
                    attempt_idx, mid_try, att["provider"], trace_id, e,
                )
                if attempt_idx < len(attempts) - 1:
                    continue
                raise HTTPException(
                    status_code=502,
                    detail=(f"无法连接 LLM 上游 {att['api_base']}。原始错误: {e!s}")[:2000],
                )
            except httpx.TimeoutException as e:
                _record_model_timeout(f"{mid_try}@{att['provider']}")
                last_timeout_error = e
                logger.warning(
                    "[sutui-chat] 上游超时 attempt=%s model=%s provider=%s timeout=%.0fs trace_id=%s",
                    attempt_idx, mid_try, att["provider"], att["timeout"], trace_id,
                )
                if attempt_idx < len(attempts) - 1:
                    continue
                raise HTTPException(status_code=504, detail=f"LLM 上游响应超时: {e!s}"[:2000])

            _record_model_success(f"{mid_try}@{att['provider']}")

            try:
                data = r.json()
            except Exception:
                data = None

            if not att_is_direct and _sutui_chat_abort_model_fallback(r.status_code, data if isinstance(data, dict) else {}):
                winning_model = mid_try
                winning_is_direct = att_is_direct
                winning_provider = att["provider"]
                break

            if _openai_nonstream_completion_usable(data, r.status_code):
                if _openai_completion_missing_tool_calls(data, body):
                    _forced_ok = False
                    if not body.get("_tool_forced"):
                        logger.info(
                            "[chat_trace] trace_id=%s tool_calls_missing model=%s provider=%s "
                            "→ retry tool_choice=required (fake_text=%s)",
                            trace_id, mid_try, att["provider"], _response_has_fake_tool_text(data),
                        )
                        body["tool_choice"] = "required"
                        body["_tool_forced"] = True
                        try:
                            r2 = await client.post(att_url, json=body, headers=att_headers)
                            d2 = r2.json()
                        except Exception as e2:
                            logger.warning("[sutui-chat] tool_choice=required retry failed: %s", e2)
                            d2 = None
                        finally:
                            body.pop("_tool_forced", None)
                            body["tool_choice"] = "auto"
                        if d2 and _openai_nonstream_completion_usable(d2, getattr(r2, "status_code", 0)):
                            if not _openai_completion_missing_tool_calls(d2, body):
                                data = d2
                                r = r2
                                _forced_ok = True
                                logger.info(
                                    "[chat_trace] trace_id=%s tool_choice=required succeeded model=%s provider=%s",
                                    trace_id, mid_try, att["provider"],
                                )
                    if not _forced_ok:
                        logger.warning(
                            "[chat_trace] trace_id=%s tool_calls_missing model=%s provider=%s after forced retry, "
                            "fallback to next (fake_text=%s)",
                            trace_id, mid_try, att["provider"], _response_has_fake_tool_text(data),
                        )
                        if attempt_idx < len(attempts) - 1:
                            continue
                winning_model = mid_try
                winning_is_direct = att_is_direct
                winning_provider = att["provider"]
                model_id = mid_try
                break

            if attempt_idx < len(attempts) - 1:
                _prev = ""
                if isinstance(data, dict):
                    _prev = json.dumps(data, ensure_ascii=False)[:400]
                else:
                    _prev = (r.text or "")[:400]
                logger.warning(
                    "[chat_trace] trace_id=%s fallback http=%s from=%s(%s) to=%s(%s) preview=%s",
                    trace_id, r.status_code,
                    mid_try, att["provider"],
                    attempts[attempt_idx + 1]["model"], attempts[attempt_idx + 1]["provider"],
                    _prev,
                )
                continue
            winning_model = mid_try
            winning_is_direct = att_is_direct
            winning_provider = att["provider"]
            break

        if r is None:
            if last_connect_error:
                raise HTTPException(status_code=502, detail=f"无法连接 LLM 上游: {last_connect_error!s}"[:2000])
            if last_timeout_error:
                raise HTTPException(status_code=504, detail=f"LLM 上游响应超时: {last_timeout_error!s}"[:2000])
            raise HTTPException(status_code=502, detail="LLM 上游无响应")

        if data is None:
            raise HTTPException(status_code=502, detail=(r.text or "")[:2000])

        _route_label = "direct" if winning_is_direct else (winning_provider or "xskill")
        logger.info(
            "[chat_trace] trace_id=%s path=sutui_chat upstream roundtrip http=%s model=%s route=%s summary=%s",
            trace_id, r.status_code, winning_model or "-", _route_label,
            _sutui_chat_upstream_body_for_log(data if isinstance(data, dict) else None),
        )
        _audit_snap = extract_upstream_billing_snapshot(data if isinstance(data, dict) else None)
        _audit_err = ""
        if isinstance(data, dict):
            _e = _upstream_chat_error_dict(data)
            if isinstance(_e, dict) and _e.get("message"):
                _audit_err = str(_e.get("message"))[:3000]
        log_xskill_http(
            phase="sutui_chat_completions",
            method="POST",
            url=url,
            http_status=r.status_code,
            capability_or_model=winning_model or "-",
            billing_snapshot=_audit_snap if _audit_snap else None,
            error_message=_audit_err,
            extra={
                "trace_id": trace_id,
                "user_id": current_user.id,
                "stream": False,
                "user_lobster_credits": user_bal_str,
                "route": _route_label,
                "model_candidates_tried": [(a["model"], a["provider"]) for a in attempts],
            },
            bearer_token=token,
            sutui_pool=sutui_pool or "",
            upstream_response=data if isinstance(data, dict) else (r.text or None),
            outbound_request_json=out_req_for_audit,
        )

        if r.status_code == 200 and winning_model and _openai_nonstream_completion_usable(data, r.status_code):
            usage_raw = data.get("usage") if isinstance(data, dict) else None
            usage_bill = _ensure_chat_usage_for_billing(
                body,
                usage_raw if isinstance(usage_raw, dict) else None,
            )
            _apply_chat_deduct(
                db,
                current_user,
                winning_model,
                usage_bill,
                data if isinstance(data, dict) else None,
                billing_recon=chat_billing_recon,
                trace_id=trace_id,
                is_direct_api=winning_is_direct,
            )
        else:
            logger.info(
                "[chat_trace] trace_id=%s deduct=skipped_nonstream reason=%s http=%s model=%r route=%s",
                trace_id,
                "upstream_not_200" if r.status_code != 200 else "not_usable",
                r.status_code, winning_model, _route_label,
            )

        out = data
        if isinstance(out, dict) and r.status_code == 200:
            _strip_fake_tool_text_from_response(out)
        resp_status = r.status_code
        if r.status_code in (402, 403):
            out = _normalize_upstream_xskill_pool_errors_for_client(data)
            if r.status_code == 403 and _xskill_upstream_pool_quota_error(data):
                resp_status = 503
        elif resp_status >= 400:
            out = _normalize_upstream_xskill_pool_errors_for_client(data)
        return JSONResponse(
            content=out,
            status_code=resp_status,
            headers={TRACE_HEADER: trace_id},
        )

    billing_user_id = int(current_user.id)
    billing_model_holder: List[str] = [model_id]
    billing_is_direct_holder: List[bool] = [False]

    async def gen() -> AsyncIterator[bytes]:
        line_buf = bytearray()
        last_usage: Optional[Dict[str, Any]] = None
        stream_upstream_billing: Dict[str, Any] = {}
        stream_completed_ok = False
        try:
            for cand_idx, att in enumerate(attempts):
                mid_try = att["model"]
                _epfx = att.get("endpoint_prefix", "/v1")
                att_url = f"{att['api_base']}{_epfx}/chat/completions"
                att_headers = {
                    "Authorization": f"Bearer {att['api_key']}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                }
                att_is_direct = att["is_direct"]
                body["model"] = mid_try
                try:
                    if att_is_direct:
                        stream_client = _get_direct_client(att["provider"], timeout=att["timeout"])
                    else:
                        stream_client = _get_xskill_client(timeout=att["timeout"])
                    async with stream_client.stream("POST", att_url, json=body, headers=att_headers) as resp:
                            if resp.status_code >= 400:
                                txt = (await resp.aread()).decode("utf-8", errors="replace")
                                logger.info(
                                    "[chat_trace] trace_id=%s stream_fail http=%s model=%s provider=%s preview=%s",
                                    trace_id, resp.status_code, mid_try, att["provider"],
                                    txt[:500].replace("\n", " "),
                                )
                                try:
                                    parsed_j = json.loads(txt) if txt.strip().startswith("{") else {}
                                except Exception:
                                    parsed_j = {}
                                pj = parsed_j if isinstance(parsed_j, dict) else {}
                                log_xskill_http(
                                    phase="sutui_chat_completions_stream",
                                    method="POST",
                                    url=att_url,
                                    http_status=resp.status_code,
                                    capability_or_model=mid_try or "-",
                                    billing_snapshot=None,
                                    error_message=txt[:8000],
                                    extra={
                                        "trace_id": trace_id,
                                        "user_id": current_user.id,
                                        "stream": True,
                                        "user_lobster_credits": user_bal_str,
                                        "route": att["provider"],
                                        "stream_attempt": cand_idx,
                                    },
                                    bearer_token=token,
                                    sutui_pool=sutui_pool or "",
                                    upstream_response=txt,
                                    outbound_request_json=out_req_for_audit,
                                )
                                if not att_is_direct and _sutui_chat_abort_model_fallback(resp.status_code, pj):
                                    yield _stream_upstream_error_sse_bytes(resp.status_code, txt)
                                    return
                                if cand_idx < len(attempts) - 1:
                                    logger.warning(
                                        "[chat_trace] trace_id=%s stream_fallback http=%s from=%s(%s) to=%s(%s)",
                                        trace_id, resp.status_code,
                                        mid_try, att["provider"],
                                        attempts[cand_idx + 1]["model"], attempts[cand_idx + 1]["provider"],
                                    )
                                    continue
                                yield _stream_upstream_error_sse_bytes(resp.status_code, txt)
                                return

                            billing_model_holder[0] = mid_try
                            billing_is_direct_holder[0] = att_is_direct
                            stream_completed_ok = True
                            _record_model_success(f"{mid_try}@{att['provider']}")
                            logger.info(
                                "[chat_trace] trace_id=%s stream_started http=200 model=%s provider=%s",
                                trace_id, mid_try or "-", att["provider"],
                            )
                            log_xskill_http(
                                phase="sutui_chat_completions_stream",
                                method="POST",
                                url=att_url,
                                http_status=200,
                                capability_or_model=mid_try or "-",
                                billing_snapshot={"note": "stream started"},
                                error_message="",
                                extra={
                                    "trace_id": trace_id,
                                    "user_id": current_user.id,
                                    "stream": True,
                                    "user_lobster_credits": user_bal_str,
                                    "route": att["provider"],
                                },
                                bearer_token=token,
                                sutui_pool=sutui_pool or "",
                                upstream_response={"note": "SSE stream started", "trace_id": trace_id},
                                outbound_request_json=out_req_for_audit,
                            )
                            async for chunk in resp.aiter_bytes():
                                yield chunk
                                line_buf.extend(chunk)
                                while True:
                                    nl = line_buf.find(b"\n")
                                    if nl < 0:
                                        break
                                    line_bytes = line_buf[:nl].rstrip(b"\r")
                                    del line_buf[: nl + 1]
                                    line = line_bytes.decode("utf-8", errors="replace").strip()
                                    if not line.startswith("data:"):
                                        continue
                                    payload = line[5:].strip()
                                    if not payload or payload == "[DONE]":
                                        continue
                                    try:
                                        obj = json.loads(payload)
                                    except json.JSONDecodeError:
                                        continue
                                    if not isinstance(obj, dict):
                                        continue
                                    for _bk in ("x_billing", "X-Billing"):
                                        if _bk in obj and obj[_bk] is not None:
                                            stream_upstream_billing[_bk] = obj[_bk]
                                    u = obj.get("usage")
                                    if isinstance(u, dict) and (
                                        u.get("prompt_tokens") is not None
                                        or u.get("completion_tokens") is not None
                                        or u.get("total_tokens") is not None
                                    ):
                                        last_usage = u
                            break
                except httpx.ConnectError as e:
                    if cand_idx < len(attempts) - 1:
                        logger.warning(
                            "[sutui-chat] 流式连接失败 trace_id=%s model=%s provider=%s err=%s",
                            trace_id, mid_try, att["provider"], e,
                        )
                        continue
                    err = json.dumps(
                        {"error": {"message": f"无法连接 LLM 上游 {att['api_base']}: {e!s}"[:2000], "status": 502}},
                        ensure_ascii=False,
                    )
                    yield f"data: {err}\n\n".encode("utf-8")
                except httpx.TimeoutException as e:
                    _record_model_timeout(f"{mid_try}@{att['provider']}")
                    if cand_idx < len(attempts) - 1:
                        logger.warning(
                            "[sutui-chat] 流式超时 trace_id=%s model=%s provider=%s timeout=%.0fs",
                            trace_id, mid_try, att["provider"], att["timeout"],
                        )
                        continue
                    err = json.dumps(
                        {"error": {"message": f"LLM 上游超时: {e!s}"[:2000], "status": 504}},
                        ensure_ascii=False,
                    )
                    yield f"data: {err}\n\n".encode("utf-8")
        finally:
            bill_model = billing_model_holder[0]
            if not stream_completed_ok or not bill_model or not _should_deduct_credits():
                logger.info(
                    "[chat_trace] trace_id=%s path=sutui_chat stream_deduct=skipped stream_ok=%s model_id=%r "
                    "should_deduct=%s had_usage_chunk=%s had_upstream_billing_chunk=%s",
                    trace_id,
                    stream_completed_ok,
                    bill_model,
                    _should_deduct_credits(),
                    last_usage is not None,
                    bool(stream_upstream_billing),
                )
                return
            usage_for_deduct = _ensure_chat_usage_for_billing(body, last_usage)
            resp_for_bill: Dict[str, Any] = {"usage": usage_for_deduct, **stream_upstream_billing}
            db_bill = SessionLocal()
            try:
                u2 = db_bill.query(User).filter(User.id == billing_user_id).first()
                if not u2:
                    logger.error(
                        "[sutui-chat] 流式结束后扣费跳过：用户不存在 trace_id=%s user_id=%s",
                        trace_id,
                        billing_user_id,
                    )
                else:
                    _apply_chat_deduct(
                        db_bill,
                        u2,
                        bill_model,
                        usage_for_deduct if isinstance(usage_for_deduct, dict) else None,
                        resp_for_bill,
                        billing_recon=chat_billing_recon,
                        trace_id=trace_id,
                        is_direct_api=billing_is_direct_holder[0],
                    )
            except HTTPException as exc:
                if exc.status_code == 402:
                    logger.error(
                        "[chat_trace] trace_id=%s path=sutui_chat stream_deduct=failed_insufficient user_id=%s model=%s detail=%s",
                        trace_id,
                        billing_user_id,
                        bill_model,
                        exc.detail,
                    )
                    logger.error(
                        "[sutui-chat] 流式结束后扣费失败 trace_id=%s user_id=%s model=%s detail=%s",
                        trace_id,
                        billing_user_id,
                        bill_model,
                        exc.detail,
                    )
                else:
                    logger.exception(
                        "[sutui-chat] 流式结束后扣费异常 trace_id=%s user_id=%s model=%s",
                        trace_id,
                        billing_user_id,
                        bill_model,
                    )
            except Exception:
                logger.exception(
                    "[sutui-chat] 流式结束后扣费异常 trace_id=%s user_id=%s model=%s",
                    trace_id,
                    billing_user_id,
                    bill_model,
                )
            finally:
                db_bill.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={TRACE_HEADER: trace_id},
    )
