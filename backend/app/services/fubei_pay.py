"""付呗聚合支付：签名、请求、预下单、订单查询。

网关地址: https://shq-api.51fubei.com/gateway/agent
文档: https://www.yuque.com/51fubei/openapi
官方 Python SDK: https://gitee.com/fubei-open/python-sdk
"""

import hashlib
import json
import logging
import uuid
from typing import Any, Optional

import httpx

from ..core.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


def _cfg_app_id() -> str:
    return (getattr(settings, "fubei_app_id", None) or "").strip()


def _cfg_app_secret() -> str:
    return (getattr(settings, "fubei_app_secret", None) or "").strip()


def _cfg_gateway_url() -> str:
    return (getattr(settings, "fubei_gateway_url", None) or "").strip() or "https://shq-api.51fubei.com/gateway/agent"


def fubei_configured() -> bool:
    return bool(_cfg_app_id() and _cfg_app_secret())


def fubei_sign(params: dict[str, Any], app_secret: str) -> str:
    """付呗签名：所有参数（除 sign）按 key ASCII 排序，拼接 &，尾部追加 app_secret，MD5 大写。"""
    parts = []
    for k in sorted(params.keys()):
        if k == "sign":
            continue
        parts.append(f"{k}={params[k]}")
    raw = "&".join(parts) + app_secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _build_request_body(method: str, biz_content: dict[str, Any]) -> dict[str, Any]:
    app_id = _cfg_app_id()
    app_secret = _cfg_app_secret()
    nonce = uuid.uuid4().hex[:24]
    body = {
        "app_id": app_id,
        "method": method,
        "format": "json",
        "sign_method": "md5",
        "nonce": nonce,
        "version": "1.0",
        "biz_content": json.dumps(biz_content, ensure_ascii=False),
    }
    body["sign"] = fubei_sign(body, app_secret)
    return body


async def fubei_request(method: str, biz_content: dict[str, Any]) -> dict[str, Any]:
    """向付呗网关发送请求，返回解析后的 JSON 响应。

    成功时 result_code=200，业务数据在 data 字段。
    """
    url = _cfg_gateway_url()
    body = _build_request_body(method, biz_content)
    logger.info("[fubei] request method=%s url=%s biz_keys=%s", method, url, list(biz_content.keys()))
    async with httpx.AsyncClient(timeout=_TIMEOUT, trust_env=False) as client:
        resp = await client.post(url, json=body, headers={"Content-Type": "application/json; charset=utf-8"})
    resp.raise_for_status()
    result = resp.json()
    code = result.get("result_code")
    if code != 200:
        logger.warning("[fubei] method=%s result_code=%s message=%s", method, code, result.get("result_message"))
    return result


async def fubei_precreate(
    merchant_order_sn: str,
    total_amount: float,
    body: str = "",
    notify_url: str = "",
    success_url: str = "",
    fail_url: str = "",
    cancel_url: str = "",
    timeout_express: str = "",
    attach: str = "",
) -> dict[str, Any]:
    """预下单 → 聚合收款码 (C扫B)。

    返回付呗 result dict；成功时 data 中含 qr_code / order_sn 等。
    """
    biz: dict[str, Any] = {
        "merchant_order_sn": merchant_order_sn,
        "total_amount": total_amount,
    }
    store_id = getattr(settings, "fubei_store_id", None)
    if store_id:
        biz["store_id"] = int(store_id)
    if body:
        biz["body"] = body[:128]
    if notify_url:
        biz["notify_url"] = notify_url[:255]
    if success_url:
        biz["success_url"] = success_url[:255]
    if fail_url:
        biz["fail_url"] = fail_url[:255]
    if cancel_url:
        biz["cancel_url"] = cancel_url[:255]
    if timeout_express:
        biz["timeout_express"] = timeout_express
    if attach:
        biz["attach"] = attach[:127]
    return await fubei_request("fbpay.order.precreate", biz)


async def fubei_query_order(
    merchant_order_sn: Optional[str] = None,
    order_sn: Optional[str] = None,
) -> dict[str, Any]:
    """查询订单状态。merchant_order_sn 或 order_sn 二选一。"""
    biz: dict[str, Any] = {}
    if merchant_order_sn:
        biz["merchant_order_sn"] = merchant_order_sn
    if order_sn:
        biz["order_sn"] = order_sn
    return await fubei_request("fbpay.order.query", biz)


async def fubei_close_order(
    merchant_order_sn: Optional[str] = None,
    order_sn: Optional[str] = None,
) -> dict[str, Any]:
    """关闭未支付订单。"""
    biz: dict[str, Any] = {}
    if merchant_order_sn:
        biz["merchant_order_sn"] = merchant_order_sn
    if order_sn:
        biz["order_sn"] = order_sn
    return await fubei_request("fbpay.order.close", biz)


def verify_callback_sign(params: dict[str, Any]) -> bool:
    """校验付呗异步回调签名。"""
    sign = (params.get("sign") or "").strip()
    if not sign:
        return False
    computed = fubei_sign(params, _cfg_app_secret())
    return computed == sign.upper()
