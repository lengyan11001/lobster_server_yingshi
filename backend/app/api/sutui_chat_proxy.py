"""鉴权后统一使用服务器赞助/管理端速推 Token 池，转发 OpenAI 兼容 chat/completions 至 api.xskill.ai。"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
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
from ..services.credits_amount import credits_json_float, quantize_credits, user_balance_decimal
from ..services.sutui_pricing import (
    estimate_credits_from_pricing,
    estimate_pre_deduct_credits,
    extract_upstream_billing_snapshot,
    extract_upstream_reported_credits,
    fetch_model_pricing,
)
from .auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


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


def _normalize_upstream_402_for_client(data: Any) -> Any:
    """
    上游 402 + insufficient_balance 表示「管理端速推 Token 在 xskill 侧余额不足」，
    与龙虾用户积分无关；替换为明确中文，避免用户误以为个人积分问题。
    """
    if not isinstance(data, dict):
        return data
    err = _upstream_chat_error_dict(data)
    if not isinstance(err, dict):
        return data
    code = str(err.get("code") or "").strip().lower()
    typ = str(err.get("type") or "").strip().lower()
    msg = str(err.get("message") or "").strip().lower()
    if code == "insufficient_balance" or typ == "billing_error" or "insufficient" in msg:
        return {
            "error": {
                "message": (
                    "速推服务端账户余额不足：当前对话使用服务器托管的速推（xskill）Token 池，"
                    "该池在速推侧余额不足，需管理员在速推控制台为对应账户充值或更换有效 Token。"
                    "若你个人龙虾积分仍充足，属于平台侧速推账户问题，而非你账号。"
                ),
                "type": "billing_error",
                "code": "upstream_insufficient_balance",
            }
        }
    return data


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


def _require_balance_before_upstream_chat(
    db: Session,
    current_user: User,
    model_id: str,
    body: Dict[str, Any],
) -> None:
    """
    在上游调用前校验余额：仅当 docs 能取到 pricing 时按本次请求做保守预估。
    无定价时不拦截（与原先「仍转发、事后按返回计费」一致），避免误杀。
    """
    if not _should_deduct_credits() or not (model_id or "").strip():
        return
    pricing = fetch_model_pricing(model_id)
    if not pricing:
        return
    params = _chat_balance_precheck_params(body)
    need = estimate_credits_from_pricing(pricing, params)
    if need <= 0:
        return
    db.refresh(current_user)
    bal = user_balance_decimal(current_user)
    if bal < need:
        raise HTTPException(
            status_code=402,
            detail=(
                f"积分不足：按本次请求参数预估至少需 {need} 积分，当前余额 {bal}。"
                "请充值或缩短上下文/降低 max_tokens 后重试。"
            ),
        )


def _credits_for_sutui_chat(
    model: str,
    usage: Optional[dict],
    response_body: Optional[Dict[str, Any]] = None,
):
    """按上游响应内嵌的本次消耗（若有）优先；否则按速推 docs 定价 + usage 计算。返回 4 位小数积分。"""
    if response_body and isinstance(response_body, dict):
        reported = extract_upstream_reported_credits(response_body)
        if reported > 0:
            return quantize_credits(reported)
    pricing = fetch_model_pricing(model)
    if not pricing:
        est, err = estimate_pre_deduct_credits(model, None)
        if err:
            logger.warning("[sutui-chat] 无定价 model=%s err=%s", model, err)
            return Decimal(0)
        return quantize_credits(est)
    params: Dict[str, Any] = {}
    if usage and isinstance(usage, dict):
        params["prompt_tokens"] = usage.get("prompt_tokens", 0)
        params["completion_tokens"] = usage.get("completion_tokens", 0)
    est = estimate_credits_from_pricing(pricing, params)
    if est <= 0:
        est2, err = estimate_pre_deduct_credits(model, None)
        if err:
            return Decimal(0)
        return quantize_credits(est2)
    return quantize_credits(est)


def _apply_chat_deduct(
    db: Session,
    current_user: User,
    model: str,
    usage: Optional[dict],
    response_body: Optional[Dict[str, Any]] = None,
) -> None:
    if not _should_deduct_credits():
        return
    reported_raw = None
    if response_body and isinstance(response_body, dict):
        reported_raw = extract_upstream_reported_credits(response_body)
    credits = _credits_for_sutui_chat(model, usage, response_body)
    billing_src = (
        "upstream价字段优先"
        if reported_raw and reported_raw > 0
        else ("docs定价+usage或兜底" if credits > 0 else "未扣费")
    )
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
        return
    db.refresh(current_user)
    bal = user_balance_decimal(current_user)
    if bal < credits:
        logger.error(
            "[sutui-chat] 扣积分失败（余额不足），上游已成功返回，不向客户端透传正文 user_id=%s model=%s need=%s have=%s",
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
    append_credit_ledger(
        db,
        current_user.id,
        -credits,
        "sutui_chat",
        bal_after,
        description=f"速推 LLM 对话扣费 model={model}",
        ref_type="sutui_chat",
        meta={
            "model": model,
            "usage": usage,
            "deduct_credits": credits_json_float(credits),
        },
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

    _remap_sutui_chat_model(body)

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

    _require_balance_before_upstream_chat(db, current_user, model_id, body)

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

        out = _normalize_upstream_402_for_client(data) if r.status_code == 402 else data
        return JSONResponse(content=out, status_code=r.status_code)

    # 流式：边下边解析 SSE 行，取最后一个含 usage 的 data JSON；若无则按与预检一致的保守 usage 估算扣费。

    async def gen() -> AsyncIterator[bytes]:
        line_buf = bytearray()
        last_usage: Optional[Dict[str, Any]] = None
        stream_completed_ok = False
        try:
            async with httpx.AsyncClient(timeout=300.0, trust_env=True) as client:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code >= 400:
                        txt = (await resp.aread()).decode("utf-8", errors="replace")
                        if resp.status_code == 402:
                            try:
                                parsed = json.loads(txt) if txt.strip().startswith("{") else {}
                            except Exception:
                                parsed = {}
                            norm = _normalize_upstream_402_for_client(parsed if isinstance(parsed, dict) else {})
                            if isinstance(norm, dict) and norm.get("error"):
                                err = json.dumps(norm, ensure_ascii=False)
                            else:
                                err = json.dumps(
                                    {
                                        "error": {
                                            "message": (
                                                "速推服务端账户余额不足（流式上游返回 402）。"
                                                "需管理员在速推控制台为服务器 Token 池对应账户充值。"
                                            ),
                                            "type": "billing_error",
                                            "code": "upstream_insufficient_balance",
                                        }
                                    },
                                    ensure_ascii=False,
                                )
                        else:
                            err = json.dumps({"error": {"message": txt[:2000], "status": resp.status_code}}, ensure_ascii=False)
                        yield f"data: {err}\n\n".encode("utf-8")
                        return
                    stream_completed_ok = True
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
                            u = obj.get("usage")
                            if isinstance(u, dict) and (
                                u.get("prompt_tokens") is not None
                                or u.get("completion_tokens") is not None
                                or u.get("total_tokens") is not None
                            ):
                                last_usage = u
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
        finally:
            if not stream_completed_ok or not model_id or not _should_deduct_credits():
                return
            usage_for_deduct: Optional[Dict[str, Any]] = last_usage
            if not usage_for_deduct:
                sp = _chat_balance_precheck_params(body)
                usage_for_deduct = {
                    "prompt_tokens": sp["prompt_tokens"],
                    "completion_tokens": sp["completion_tokens"],
                }
            resp_for_bill: Dict[str, Any] = {"usage": last_usage} if last_usage else {"usage": usage_for_deduct}
            try:
                _apply_chat_deduct(
                    db,
                    current_user,
                    model_id,
                    usage_for_deduct if isinstance(usage_for_deduct, dict) else None,
                    resp_for_bill,
                )
            except HTTPException as exc:
                if exc.status_code == 402:
                    logger.error(
                        "[sutui-chat] 流式结束后扣费失败 user_id=%s model=%s detail=%s",
                        current_user.id,
                        model_id,
                        exc.detail,
                    )
                else:
                    logger.exception(
                        "[sutui-chat] 流式结束后扣费异常 user_id=%s model=%s",
                        current_user.id,
                        model_id,
                    )

    return StreamingResponse(gen(), media_type="text/event-stream")
