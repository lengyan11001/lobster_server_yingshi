"""互亿无线短信（验证码），配置见 IHUYI_SMS_ACCOUNT / IHUYI_SMS_PASSWORD。"""
from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

IHUYI_SUBMIT_URL = "https://api.ihuyi.com/sms/Submit.json"


def send_verify_code_sms(*, account: str, api_key: str, mobile: str, code: str) -> Dict[str, Any]:
    """
    调用互亿 Submit.json；成功时响应 JSON 通常含 code=2。
    account=APIID，api_key=APIKEY（文档中的 password 字段）。
    """
    content = f"您的验证码是：{code}。请不要把验证码泄露给其他人。"
    data = {
        "account": account,
        "password": api_key,
        "mobile": mobile,
        "content": content,
    }
    headers = {
        "Content-type": "application/x-www-form-urlencoded",
        "Accept": "text/plain",
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(IHUYI_SUBMIT_URL, data=data, headers=headers)
        r.raise_for_status()
        out = r.json()
    except Exception as e:
        logger.exception("ihuyi sms request failed: %s", e)
        raise RuntimeError("短信通道请求失败，请稍后重试") from e
    # 互亿：2 为成功；部分账号返回整数或字符串
    c = out.get("code")
    try:
        code_int = int(c) if c is not None else -1
    except (TypeError, ValueError):
        code_int = -1
    if code_int == 2:
        return out
    msg = out.get("msg") or out.get("message") or str(out)
    logger.warning("ihuyi sms business error: %s", out)
    raise RuntimeError(msg if isinstance(msg, str) else "短信发送失败")
