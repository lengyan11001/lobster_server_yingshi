"""Twilio WhatsApp（海外/大陆均可部署）：配置 JSON + 入站 Webhook 验签与 TwiML。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ..core.config import settings
from .auth import get_messenger_user_id

router = APIRouter()
logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_TWILIO_CONFIG_PATH = _BASE_DIR / "twilio_whatsapp_config.json"
_INBOUND_PATH = "/api/twilio/whatsapp/inbound"


def _read_twilio_file() -> dict:
    if not _TWILIO_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_TWILIO_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_twilio_file(data: dict) -> None:
    _TWILIO_CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _mask_sid(s: str) -> str:
    s = (s or "").strip()
    if len(s) <= 8:
        return "****" if s else ""
    return s[:4] + "…" + s[-4:]


def _mask_token_set() -> bool:
    return bool((_read_twilio_file().get("auth_token") or "").strip()) or bool(
        (getattr(settings, "twilio_auth_token", None) or "").strip()
    )


def effective_auth_token() -> str:
    f = _read_twilio_file()
    return (f.get("auth_token") or "").strip() or (
        getattr(settings, "twilio_auth_token", None) or ""
    ).strip()


def effective_account_sid() -> str:
    f = _read_twilio_file()
    return (f.get("account_sid") or "").strip() or (
        getattr(settings, "twilio_account_sid", None) or ""
    ).strip()


def effective_signature_url(request: Request) -> str:
    """签名校验 URL：以服务器 .env 为准（TWILIO_WHATSAPP_WEBHOOK_FULL_URL / PUBLIC_BASE_URL），其次为旧版 JSON。"""
    path = request.url.path
    explicit = (getattr(settings, "twilio_whatsapp_webhook_full_url", None) or "").strip()
    if explicit:
        return explicit
    public = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    if public:
        return public + path
    f = _read_twilio_file()
    explicit = (f.get("webhook_full_url") or "").strip()
    if explicit:
        return explicit
    pub = (f.get("public_base") or "").strip().rstrip("/")
    if pub:
        return pub + path
    return str(request.url)


def _twilio_webhook_suggested() -> str:
    path = _INBOUND_PATH
    wh_full = (getattr(settings, "twilio_whatsapp_webhook_full_url", None) or "").strip()
    if wh_full:
        return wh_full
    env_pub = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    if env_pub:
        return env_pub + path
    f = _read_twilio_file()
    wh_full = (f.get("webhook_full_url") or "").strip()
    if wh_full:
        return wh_full
    pub = (f.get("public_base") or "").strip().rstrip("/")
    if pub:
        return pub + path
    return ""


def _form_to_str_dict(form: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key in form.keys():
        v = form.get(key)
        if v is not None:
            out[str(key)] = v if isinstance(v, str) else str(v)
    return out


class TwilioTestSendBody(BaseModel):
    to: str
    from_whatsapp: str
    body: str = "Lobster 测试"


class TwilioWhatsappConfigUpdate(BaseModel):
    account_sid: Optional[str] = None
    auth_token: Optional[str] = None


@router.get("/api/twilio-whatsapp/config", summary="读取 Twilio WhatsApp 配置（脱敏）")
def get_twilio_whatsapp_config(_: int = Depends(get_messenger_user_id)):
    f = _read_twilio_file()
    sid = (f.get("account_sid") or "").strip()
    path = _INBOUND_PATH
    suggested = _twilio_webhook_suggested()
    env_pub = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    return {
        "account_sid_masked": _mask_sid(sid),
        "has_account_sid": bool(sid),
        "has_auth_token": _mask_token_set(),
        "public_base_effective": env_pub,
        "webhook_suggested": suggested,
        "inbound_path": path,
        "env_fallback_note": "公网与 Webhook 由服务器 .env 决定：PUBLIC_BASE_URL、TWILIO_WHATSAPP_WEBHOOK_FULL_URL、TWILIO_AUTH_TOKEN；盒内 JSON 仅保存 SID/Token",
    }


@router.post("/api/twilio-whatsapp/config", summary="保存 Twilio WhatsApp 配置（JSON，立即生效）")
def post_twilio_whatsapp_config(
    body: TwilioWhatsappConfigUpdate,
    _: int = Depends(get_messenger_user_id),
):
    f = _read_twilio_file()
    patch = body.model_dump(exclude_unset=True)
    if "account_sid" in patch:
        s = str(patch.get("account_sid") or "").strip()
        if s:
            f["account_sid"] = s
        else:
            f.pop("account_sid", None)
    if "auth_token" in patch:
        t = str(patch.get("auth_token") or "").strip()
        if t:
            f["auth_token"] = t
        else:
            f.pop("auth_token", None)
    _write_twilio_file(f)
    logger.info("[Twilio WA] 配置已更新 path=%s", _TWILIO_CONFIG_PATH)
    return {"ok": True, "message": "Twilio WhatsApp 配置已保存并生效"}


@router.post("/api/twilio-whatsapp/test-send", summary="Twilio 出站 WhatsApp 测试")
def twilio_whatsapp_test_send(
    body: TwilioTestSendBody,
    _: int = Depends(get_messenger_user_id),
):
    sid = effective_account_sid()
    token = effective_auth_token()
    if not sid or not token:
        raise HTTPException(
            status_code=400,
            detail="请先保存 Account SID 与 Auth Token，或配置环境变量",
        )
    to = body.to.strip()
    from_w = body.from_whatsapp.strip()
    text = (body.body or "Lobster 测试").strip()
    if not to.startswith("whatsapp:") or not from_w.startswith("whatsapp:"):
        raise HTTPException(
            status_code=400,
            detail="From / To 须为 whatsapp:+E164 格式",
        )
    from twilio.rest import Client

    client = Client(sid, token)
    try:
        msg = client.messages.create(from_=from_w, to=to, body=text)
    except Exception as e:
        logger.warning("[Twilio WA] test-send 失败: %s", e)
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"ok": True, "message_sid": msg.sid if msg else ""}


@router.post(_INBOUND_PATH, summary="Twilio WhatsApp 入站 Webhook")
async def twilio_whatsapp_inbound(request: Request):
    token = effective_auth_token()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="未配置 Auth Token（twilio_whatsapp_config.json 或 TWILIO_AUTH_TOKEN）",
        )
    try:
        form = await request.form()
    except Exception as e:
        logger.warning("[Twilio WA] 解析表单失败: %s", e)
        raise HTTPException(status_code=400, detail="invalid form body") from e

    params = _form_to_str_dict(form)
    sig = (request.headers.get("X-Twilio-Signature") or "").strip()
    if not sig:
        raise HTTPException(status_code=403, detail="missing X-Twilio-Signature")

    from twilio.request_validator import RequestValidator

    url = effective_signature_url(request)
    if not RequestValidator(token).validate(url, params, sig):
        logger.warning("[Twilio WA] 签名校验失败 url=%s", url)
        raise HTTPException(status_code=403, detail="invalid Twilio signature")

    frm = params.get("From", "")
    to = params.get("To", "")
    body = (params.get("Body") or "").strip()
    num_media = params.get("NumMedia", "0")

    logger.info(
        "[Twilio WA] inbound From=%s To=%s NumMedia=%s Body=%s",
        frm,
        to,
        num_media,
        body[:200] if body else "",
    )

    if body:
        reply = f"Lobster 已收到：{body}"
    else:
        reply = "Lobster 已收到消息（无文本正文，可能为媒体或系统事件）"

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Message>"
        f"{escape(reply)}"
        "</Message></Response>"
    )
    return Response(content=xml, media_type="application/xml; charset=utf-8")
