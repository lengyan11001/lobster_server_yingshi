"""鉴权后统一使用服务器赞助/管理端速推 Token 池，转发 OpenAI 兼容 chat/completions 至 api.xskill.ai。"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from mcp.sutui_tokens import next_sutui_server_token

from ..core.config import settings
from ..db import get_db
from ..models import User
from ..services.credit_ledger import append_credit_ledger
from ..services.sutui_pricing import (
    estimate_credits_from_pricing,
    estimate_pre_deduct_credits,
    extract_upstream_reported_credits,
    fetch_model_pricing,
)
from .auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

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


def _should_deduct_credits() -> bool:
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    return edition == "online" and getattr(settings, "lobster_independent_auth", True)


def _credits_for_sutui_chat(
    model: str,
    usage: Optional[dict],
    response_body: Optional[Dict[str, Any]] = None,
) -> int:
    """按上游响应内嵌的本次消耗（若有）优先；否则按速推 docs 定价 + usage 计算。"""
    if response_body and isinstance(response_body, dict):
        reported = extract_upstream_reported_credits(response_body)
        if reported > 0:
            return reported
    pricing = fetch_model_pricing(model)
    if not pricing:
        est, err = estimate_pre_deduct_credits(model, None)
        if err:
            logger.warning("[sutui-chat] 无定价 model=%s err=%s", model, err)
            return 0
        return est
    params: Dict[str, Any] = {}
    if usage and isinstance(usage, dict):
        params["prompt_tokens"] = usage.get("prompt_tokens", 0)
        params["completion_tokens"] = usage.get("completion_tokens", 0)
    est = estimate_credits_from_pricing(pricing, params)
    if est <= 0:
        est2, err = estimate_pre_deduct_credits(model, None)
        if err:
            return 0
        return est2
    return est


def _apply_chat_deduct(
    db: Session,
    current_user: User,
    model: str,
    usage: Optional[dict],
    response_body: Optional[Dict[str, Any]] = None,
) -> None:
    if not _should_deduct_credits():
        return
    reported_raw = 0
    if response_body and isinstance(response_body, dict):
        reported_raw = extract_upstream_reported_credits(response_body)
    credits = _credits_for_sutui_chat(model, usage, response_body)
    billing_src = (
        "upstream价字段优先"
        if reported_raw > 0
        else ("docs定价+usage或兜底" if credits > 0 else "未扣费")
    )
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
        return
    db.refresh(current_user)
    bal = current_user.credits or 0
    if bal < credits:
        logger.error(
            "[sutui-chat] 扣积分失败（余额不足） user_id=%s model=%s need=%s have=%s",
            current_user.id,
            model,
            credits,
            bal,
        )
        return
    current_user.credits = bal - credits
    bal_after = int(current_user.credits or 0)
    append_credit_ledger(
        db,
        current_user.id,
        -credits,
        "sutui_chat",
        bal_after,
        description=f"速推 LLM 对话扣费 model={model}",
        ref_type="sutui_chat",
        meta={"model": model, "usage": usage},
    )
    db.commit()
    logger.info("[sutui-chat] 已扣积分 user_id=%s model=%s credits=%s", current_user.id, model, credits)


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

    token = await next_sutui_server_token(is_admin=True)
    if not token:
        raise HTTPException(
            status_code=503,
            detail="服务器未配置速推 Token 池（请配置 SUTUI_SERVER_TOKENS_ADMIN / SUTUI_SERVER_TOKEN_ADMIN 或兼容项 SUTUI_SERVER_TOKEN）",
        )

    stream = bool(body.get("stream"))
    url = f"{_api_base()}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    model_id = (body.get("model") or "").strip()

    if not stream:
        try:
            async with httpx.AsyncClient(timeout=120.0, trust_env=True) as client:
                r = await client.post(url, json=body, headers=headers)
        except httpx.ConnectError as e:
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
            logger.exception("[sutui-chat] 上游请求超时 url=%s", url)
            raise HTTPException(status_code=504, detail=f"速推 LLM 上游响应超时: {e!s}"[:2000])
        try:
            data = r.json()
        except Exception:
            raise HTTPException(status_code=502, detail=(r.text or "")[:2000])

        if r.status_code == 200 and model_id:
            usage = data.get("usage") if isinstance(data, dict) else None
            _apply_chat_deduct(
                db,
                current_user,
                model_id,
                usage if isinstance(usage, dict) else None,
                data if isinstance(data, dict) else None,
            )

        return JSONResponse(content=data, status_code=r.status_code)

    # 流式：上游不在此返回完整 usage，扣费与「非流式」一致请用 stream=false；此处暂不扣积分以免失败无法回退

    async def gen() -> AsyncIterator[bytes]:
        try:
            async with httpx.AsyncClient(timeout=300.0, trust_env=True) as client:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code >= 400:
                        txt = (await resp.aread()).decode("utf-8", errors="replace")
                        err = json.dumps({"error": {"message": txt[:2000], "status": resp.status_code}}, ensure_ascii=False)
                        yield f"data: {err}\n\n".encode("utf-8")
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk
        except httpx.ConnectError as e:
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
            logger.exception("[sutui-chat] 流式上游超时 url=%s", url)
            err = json.dumps(
                {"error": {"message": f"速推 LLM 上游超时: {e!s}"[:2000], "status": 504}},
                ensure_ascii=False,
            )
            yield f"data: {err}\n\n".encode("utf-8")

    return StreamingResponse(gen(), media_type="text/event-stream")
