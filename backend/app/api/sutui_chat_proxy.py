"""鉴权后统一使用服务器赞助/管理端速推 Token 池，转发 OpenAI 兼容 chat/completions 至 api.xskill.ai。"""
from __future__ import annotations

import json
import logging
import os
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


def _remap_sutui_chat_model(body: Dict[str, Any]) -> None:
    """可选：将客户端传来的 model 映射为速推分销商侧实际有通道的 id（就地修改 body）。

    环境变量 SUTUI_CHAT_MODEL_MAP_JSON：JSON 对象，键为入站 model 字符串，值为转发到 xskill 的 model。
    典型场景：mcp/models 列出 deepseek/deepseek-chat，但 default 分销商组未挂该通道；网页智能对话能用的 id 不同，
    则在此配置 {\"deepseek/deepseek-chat\":\"你在下拉/F12 里看到的真实 id\"}。
    """
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
    mid = (body.get("model") or "").strip()
    if not mid:
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
        or (typ == "rix_api_error" and ("insufficient" in code or "预扣费" in raw_msg))
        or "insufficient" in msg
    )


def _parse_sutui_chat_fallback_chain_env() -> List[str]:
    """主→备模型顺序：默认仅 deepseek-chat；可用 SUTUI_CHAT_MODEL_FALLBACK_CHAIN_JSON 覆盖添加更多。"""
    raw = (os.environ.get("SUTUI_CHAT_MODEL_FALLBACK_CHAIN_JSON") or "").strip()
    default = ["deepseek-chat"]
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


def _sutui_chat_model_candidates(initial_model: str) -> List[str]:
    """
    对话编排：优先用入站（已 remap）的 model，再按固定链尝试其它通道，去重。
    备链中每项会先过 model map，避免名称与分销商真实 id 不一致。
    """
    seen: set[str] = set()
    out: List[str] = []
    init = (initial_model or "").strip()
    if init:
        seen.add(init)
        out.append(init)
    for fb in _parse_sutui_chat_fallback_chain_env():
        m = _remap_model_id_for_sutui(fb)
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out if out else ([init] if init else [])


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


def _openai_completion_missing_tool_calls(data: Any, request_body: Dict[str, Any]) -> bool:
    """请求中传了 tools 且 tool_choice 非 none，但响应不含 tool_calls——模型未遵从 tool 指令，值得换模型重试。"""
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
) -> Tuple[Decimal, str]:
    """LLM 实扣：上游显式价字段 → docs 定价+usage（与速推官方一致）→ 无 docs 时才按 usage×fallback。"""
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
    credits, billing_src = _credits_for_sutui_chat(model, usage, response_body)
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
    model_candidates = _sutui_chat_model_candidates(model_id)
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

    if not stream:
        r: Optional[httpx.Response] = None
        data: Any = None
        winning_model = model_id
        last_connect_error: Optional[Exception] = None
        last_timeout_error: Optional[Exception] = None

        for attempt_idx, mid_try in enumerate(model_candidates):
            body["model"] = mid_try
            try:
                async with httpx.AsyncClient(timeout=120.0, trust_env=True) as client:
                    r = await client.post(url, json=body, headers=headers)
            except httpx.ConnectError as e:
                last_connect_error = e
                logger.warning(
                    "[sutui-chat] 上游连接失败 attempt=%s model=%s trace_id=%s err=%s",
                    attempt_idx,
                    mid_try,
                    trace_id,
                    e,
                )
                if attempt_idx < len(model_candidates) - 1:
                    continue
                logger.exception("[sutui-chat] 上游连接失败（出网/DNS/防火墙/上游不可达） url=%s", url)
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"无法连接速推 LLM 上游 {_api_base()}（chat/completions）。"
                        f"请在服务器上检查：安全组/防火墙是否放行 HTTPS 出站、DNS 能否解析该域名、"
                        f"是否需要 HTTP_PROXY；也可在本机执行 curl -I {_api_base()} 验证。"
                        f" 原始错误: {e!s}"
                    )[:2000],
                )
            except httpx.TimeoutException as e:
                last_timeout_error = e
                logger.warning(
                    "[sutui-chat] 上游超时 attempt=%s model=%s trace_id=%s err=%s",
                    attempt_idx,
                    mid_try,
                    trace_id,
                    e,
                )
                if attempt_idx < len(model_candidates) - 1:
                    continue
                logger.exception("[sutui-chat] 上游请求超时 url=%s", url)
                raise HTTPException(status_code=504, detail=f"速推 LLM 上游响应超时: {e!s}"[:2000])

            try:
                data = r.json()
            except Exception:
                data = None

            if _sutui_chat_abort_model_fallback(r.status_code, data if isinstance(data, dict) else {}):
                winning_model = mid_try
                break

            if _openai_nonstream_completion_usable(data, r.status_code):
                if (
                    _openai_completion_missing_tool_calls(data, body)
                    and attempt_idx < len(model_candidates) - 1
                ):
                    logger.warning(
                        "[chat_trace] trace_id=%s path=sutui_chat tool_calls_missing model=%s "
                        "fallback_to=%s (request had tools but response had no tool_calls)",
                        trace_id,
                        mid_try,
                        model_candidates[attempt_idx + 1],
                    )
                    continue
                winning_model = mid_try
                model_id = mid_try
                break

            if attempt_idx < len(model_candidates) - 1:
                _prev = ""
                if isinstance(data, dict):
                    _prev = json.dumps(data, ensure_ascii=False)[:400]
                else:
                    _prev = (r.text or "")[:400]
                logger.warning(
                    "[chat_trace] trace_id=%s path=sutui_chat fallback_model http=%s from=%s to=%s preview=%s",
                    trace_id,
                    r.status_code,
                    mid_try,
                    model_candidates[attempt_idx + 1],
                    _prev,
                )
                continue
            winning_model = mid_try
            break

        if r is None:
            if last_connect_error:
                raise HTTPException(status_code=502, detail=f"无法连接速推 LLM 上游: {last_connect_error!s}"[:2000])
            if last_timeout_error:
                raise HTTPException(status_code=504, detail=f"速推 LLM 上游响应超时: {last_timeout_error!s}"[:2000])
            raise HTTPException(status_code=502, detail="速推 LLM 上游无响应")

        if data is None:
            raise HTTPException(status_code=502, detail=(r.text or "")[:2000])

        logger.info(
            "[chat_trace] trace_id=%s path=sutui_chat upstream_xskill roundtrip http=%s model=%s summary=%s",
            trace_id,
            r.status_code,
            winning_model or "-",
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
                "model_candidates_tried": model_candidates,
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
            )
        else:
            logger.info(
                "[chat_trace] trace_id=%s path=sutui_chat deduct=skipped_nonstream reason=%s http=%s model_id=%r usable=%s",
                trace_id,
                "upstream_not_200" if r.status_code != 200 else "not_usable_or_empty_model",
                r.status_code,
                winning_model,
                _openai_nonstream_completion_usable(data, r.status_code) if r.status_code == 200 else False,
            )

        out = data
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

    # 流式：边下边解析 SSE，取最后一个含 usage 的 data；并保留上游出现的 x_billing/X-Billing（与非流式一致，优先按 credits_used 扣）。
    # StreamingResponse 返回后，Depends(get_db) 可能在生成器尚未结束时即关闭会话，finally 内禁止再用注入的 db 扣费。
    billing_user_id = int(current_user.id)
    billing_model_holder: List[str] = [model_id]

    async def gen() -> AsyncIterator[bytes]:
        line_buf = bytearray()
        last_usage: Optional[Dict[str, Any]] = None
        # 流内任一事例带计费扩展则记录；同名字段以后出现的 chunk 覆盖（通常最后一帧最全）
        stream_upstream_billing: Dict[str, Any] = {}
        stream_completed_ok = False
        try:
            for cand_idx, mid_try in enumerate(model_candidates):
                body["model"] = mid_try
                try:
                    async with httpx.AsyncClient(timeout=300.0, trust_env=True) as client:
                        async with client.stream("POST", url, json=body, headers=headers) as resp:
                            if resp.status_code >= 400:
                                txt = (await resp.aread()).decode("utf-8", errors="replace")
                                logger.info(
                                    "[chat_trace] trace_id=%s path=sutui_chat upstream_xskill stream_fail http=%s model=%s preview=%s",
                                    trace_id,
                                    resp.status_code,
                                    mid_try,
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
                                    url=url,
                                    http_status=resp.status_code,
                                    capability_or_model=mid_try or "-",
                                    billing_snapshot=None,
                                    error_message=txt[:8000],
                                    extra={
                                        "trace_id": trace_id,
                                        "user_id": current_user.id,
                                        "stream": True,
                                        "user_lobster_credits": user_bal_str,
                                        "model_candidates_tried": model_candidates,
                                        "stream_attempt": cand_idx,
                                    },
                                    bearer_token=token,
                                    sutui_pool=sutui_pool or "",
                                    upstream_response=txt,
                                    outbound_request_json=out_req_for_audit,
                                )
                                if _sutui_chat_abort_model_fallback(resp.status_code, pj):
                                    yield _stream_upstream_error_sse_bytes(resp.status_code, txt)
                                    return
                                if cand_idx < len(model_candidates) - 1:
                                    logger.warning(
                                        "[chat_trace] trace_id=%s path=sutui_chat stream_fallback http=%s from=%s to=%s",
                                        trace_id,
                                        resp.status_code,
                                        mid_try,
                                        model_candidates[cand_idx + 1],
                                    )
                                    continue
                                yield _stream_upstream_error_sse_bytes(resp.status_code, txt)
                                return

                            billing_model_holder[0] = mid_try
                            stream_completed_ok = True
                            logger.info(
                                "[chat_trace] trace_id=%s path=sutui_chat upstream_xskill stream_started http=200 model=%s",
                                trace_id,
                                mid_try or "-",
                            )
                            log_xskill_http(
                                phase="sutui_chat_completions_stream",
                                method="POST",
                                url=url,
                                http_status=200,
                                capability_or_model=mid_try or "-",
                                billing_snapshot={"note": "stream started; usage/x_billing 在流结束后扣费日志中"},
                                error_message="",
                                extra={
                                    "trace_id": trace_id,
                                    "user_id": current_user.id,
                                    "stream": True,
                                    "user_lobster_credits": user_bal_str,
                                    "model_candidates_tried": model_candidates,
                                },
                                bearer_token=token,
                                sutui_pool=sutui_pool or "",
                                upstream_response={
                                    "note": "SSE 流已开始，完整 chunks 不在此条；请用 trace_id 对齐后续扣费流水",
                                    "trace_id": trace_id,
                                },
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
                    if cand_idx < len(model_candidates) - 1:
                        logger.warning(
                            "[sutui-chat] 流式连接失败将换模型 trace_id=%s model=%s err=%s",
                            trace_id,
                            mid_try,
                            e,
                        )
                        continue
                    logger.exception("[sutui-chat] 流式上游连接失败 url=%s", url)
                    err = json.dumps(
                        {
                            "error": {
                                "message": (
                                    f"无法连接速推 LLM 上游 {_api_base()}。请检查服务器 HTTPS 出站与 DNS。"
                                    f" 原始错误: {e!s}"
                                )[:2000],
                                "status": 502,
                            }
                        },
                        ensure_ascii=False,
                    )
                    yield f"data: {err}\n\n".encode("utf-8")
                except httpx.TimeoutException as e:
                    if cand_idx < len(model_candidates) - 1:
                        logger.warning(
                            "[sutui-chat] 流式超时将换模型 trace_id=%s model=%s err=%s",
                            trace_id,
                            mid_try,
                            e,
                        )
                        continue
                    logger.exception("[sutui-chat] 流式上游超时 url=%s", url)
                    err = json.dumps(
                        {"error": {"message": f"速推 LLM 上游超时: {e!s}"[:2000], "status": 504}},
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
