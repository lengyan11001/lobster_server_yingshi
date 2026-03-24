"""Facebook Messenger：多应用配置 CRUD + Webhook（GET 校验 / POST 收消息 + Graph 发送）。"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import string
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import get_messenger_user_id
from .chat import get_reply_for_channel
from ..core.config import settings
from ..db import get_db
from ..models import MessengerConfig

logger = logging.getLogger(__name__)
router = APIRouter()

GRAPH_API_VERSION = "v21.0"


def _random_callback_path(length: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _verify_meta_signature(app_secret: str, raw_body: bytes, signature_header: str) -> bool:
    if not signature_header.startswith("sha256="):
        return False
    got = signature_header[7:].strip()
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(got, expected)


async def _graph_send_text(page_access_token: str, psid: str, text: str) -> None:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
    payload = {"recipient": {"id": psid}, "message": {"text": text[:2000]}}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            url,
            params={"access_token": page_access_token},
            json=payload,
            headers={"Content-Type": "application/json"},
        )
    if r.status_code >= 400:
        logger.error("[Messenger] Graph 发送失败 status=%s body=%s", r.status_code, r.text[:500])
        raise HTTPException(status_code=502, detail=f"Graph API error: {r.status_code}")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
class MessengerConfigCreate(BaseModel):
    name: str = "Messenger"
    verify_token: str = Field(..., min_length=1)
    app_secret: str = Field(..., min_length=1)
    page_id: str = Field(..., min_length=1)
    page_access_token: str = Field(..., min_length=1)
    product_knowledge: Optional[str] = None
    callback_path: Optional[str] = None


class MessengerConfigUpdate(BaseModel):
    name: Optional[str] = None
    verify_token: Optional[str] = None
    app_secret: Optional[str] = None
    page_id: Optional[str] = None
    page_access_token: Optional[str] = None
    product_knowledge: Optional[str] = None


def _public_base(request) -> str:
    base = (settings.public_base_url or "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return base


@router.get("/api/messenger/configs", summary="Messenger 配置列表")
def list_messenger_configs(
    request: Request,
    user_id: int = Depends(get_messenger_user_id),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(MessengerConfig)
        .filter(MessengerConfig.user_id == user_id)
        .order_by(MessengerConfig.id)
        .all()
    )
    base = _public_base(request)
    return {
        "configs": [
            {
                "id": r.id,
                "name": r.name,
                "callback_path": r.callback_path,
                "webhook_url": f"{base}/api/messenger/callback/{r.callback_path}",
                "page_id": r.page_id,
                "has_page_token": bool((r.page_access_token or "").strip()),
                "has_app_secret": bool((r.app_secret or "").strip()),
                "has_product_knowledge": bool((r.product_knowledge or "").strip()),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post("/api/messenger/configs", summary="新增 Messenger 配置")
def create_messenger_config(
    body: MessengerConfigCreate,
    request: Request,
    user_id: int = Depends(get_messenger_user_id),
    db: Session = Depends(get_db),
):
    path = (body.callback_path or "").strip()
    if path:
        if not all(c.isalnum() for c in path) or len(path) < 6 or len(path) > 64:
            raise HTTPException(status_code=400, detail="callback_path 需为 6～64 位字母数字")
        if db.query(MessengerConfig).filter(MessengerConfig.callback_path == path).first() is not None:
            raise HTTPException(status_code=400, detail="该 callback_path 已存在")
    else:
        for _ in range(5):
            path = _random_callback_path()
            if db.query(MessengerConfig).filter(MessengerConfig.callback_path == path).first() is None:
                break
        else:
            raise HTTPException(status_code=500, detail="生成 callback_path 冲突")
    row = MessengerConfig(
        user_id=user_id,
        name=(body.name or "Messenger").strip() or "Messenger",
        callback_path=path,
        verify_token=body.verify_token.strip(),
        app_secret=body.app_secret.strip(),
        page_id=body.page_id.strip(),
        page_access_token=body.page_access_token.strip(),
        product_knowledge=(body.product_knowledge or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    base = _public_base(request)
    return {
        "id": row.id,
        "name": row.name,
        "callback_path": row.callback_path,
        "webhook_url": f"{base}/api/messenger/callback/{row.callback_path}",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/api/messenger/configs/{config_id:int}", summary="单条配置（含敏感字段，仅编辑）")
def get_messenger_config(
    config_id: int,
    user_id: int = Depends(get_messenger_user_id),
    db: Session = Depends(get_db),
):
    row = db.query(MessengerConfig).filter(
        MessengerConfig.id == config_id, MessengerConfig.user_id == user_id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    return {
        "id": row.id,
        "name": row.name,
        "callback_path": row.callback_path,
        "verify_token": row.verify_token,
        "app_secret": row.app_secret,
        "page_id": row.page_id,
        "page_access_token": row.page_access_token,
        "product_knowledge": row.product_knowledge or "",
    }


@router.put("/api/messenger/configs/{config_id:int}", summary="更新 Messenger 配置")
def update_messenger_config(
    config_id: int,
    body: MessengerConfigUpdate,
    user_id: int = Depends(get_messenger_user_id),
    db: Session = Depends(get_db),
):
    row = db.query(MessengerConfig).filter(
        MessengerConfig.id == config_id, MessengerConfig.user_id == user_id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    if body.name is not None:
        row.name = (body.name or "Messenger").strip() or "Messenger"
    if body.verify_token is not None and (body.verify_token or "").strip():
        row.verify_token = body.verify_token.strip()
    if body.app_secret is not None and (body.app_secret or "").strip():
        row.app_secret = body.app_secret.strip()
    if body.page_id is not None and (body.page_id or "").strip():
        row.page_id = body.page_id.strip()
    if body.page_access_token is not None and (body.page_access_token or "").strip():
        row.page_access_token = body.page_access_token.strip()
    if body.product_knowledge is not None:
        row.product_knowledge = (body.product_knowledge or "").strip() or None
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "callback_path": row.callback_path}


@router.delete("/api/messenger/configs/{config_id:int}", summary="删除 Messenger 配置")
def delete_messenger_config(
    config_id: int,
    user_id: int = Depends(get_messenger_user_id),
    db: Session = Depends(get_db),
):
    row = db.query(MessengerConfig).filter(
        MessengerConfig.id == config_id, MessengerConfig.user_id == user_id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
def _find_config_by_path(db: Session, callback_path: str) -> Optional[MessengerConfig]:
    return db.query(MessengerConfig).filter(MessengerConfig.callback_path == callback_path).first()


@router.get("/api/messenger/callback/{callback_path:path}", summary="Meta Webhook 校验（GET）")
async def messenger_webhook_verify(
    callback_path: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """hub.mode=subscribe & hub.verify_token & hub.challenge"""
    q = request.query_params
    mode = (q.get("hub.mode") or "").strip()
    token = (q.get("hub.verify_token") or "").strip()
    challenge = q.get("hub.challenge") or ""
    if mode != "subscribe":
        raise HTTPException(status_code=400, detail="invalid hub.mode")
    cfg = _find_config_by_path(db, callback_path)
    if not cfg:
        raise HTTPException(status_code=404, detail="config not found")
    if token != (cfg.verify_token or "").strip():
        raise HTTPException(status_code=403, detail="verify_token mismatch")
    return Response(content=challenge, media_type="text/plain")


@router.post("/api/messenger/callback/{callback_path:path}", summary="Meta Webhook 消息（POST）")
async def messenger_webhook_post(
    callback_path: str,
    request: Request,
    db: Session = Depends(get_db),
):
    cfg = _find_config_by_path(db, callback_path)
    if not cfg:
        raise HTTPException(status_code=404, detail="config not found")
    raw = await request.body()
    sig = (request.headers.get("X-Hub-Signature-256") or "").strip()
    if not _verify_meta_signature(cfg.app_secret, raw, sig):
        logger.warning("[Messenger] 签名校验失败 path=%s", callback_path)
        raise HTTPException(status_code=403, detail="invalid signature")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid json: {e}") from e
    if (data.get("object") or "") != "page":
        return Response(content="ok", media_type="text/plain")

    entries = data.get("entry") or []
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="invalid entry")
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        page_id = str(ent.get("id") or "").strip()
        expect_pid = (cfg.page_id or "").strip()
        if page_id and expect_pid and page_id != expect_pid:
            logger.warning("[Messenger] page_id 与配置不一致 expect=%s got=%s", expect_pid, page_id)
            raise HTTPException(status_code=400, detail="page_id mismatch")
        messaging_list = ent.get("messaging") or []
        if not isinstance(messaging_list, list):
            continue
        for ev in messaging_list:
            if not isinstance(ev, dict):
                continue
            if ev.get("message", {}).get("is_echo"):
                continue
            msg = ev.get("message") or {}
            if not isinstance(msg, dict):
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            sender = ev.get("sender") or {}
            psid = str(sender.get("id") or "").strip()
            if not psid:
                continue
            extra = ""
            if (cfg.product_knowledge or "").strip():
                extra = "\n【产品信息】\n" + (cfg.product_knowledge or "").strip()
            session_id = f"messenger_{cfg.id}_{psid}"
            reply_text = await get_reply_for_channel(
                text,
                session_id=session_id,
                system_prompt_extra=extra,
                channel_system="你是 Facebook Messenger 页面客服助手。根据用户消息简短、友好地回复。使用中文。",
            )
            await _graph_send_text(cfg.page_access_token, psid, reply_text)

    return Response(content="ok", media_type="text/plain")
