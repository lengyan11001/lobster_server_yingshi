"""企业微信：多应用配置 CRUD + 回调入队 + 占位回复；本地轮询拉取与提交回复，云端代发应用消息。"""
from __future__ import annotations

import logging
import random
import string
import time
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import get_current_user
from ..core.config import settings
from ..db import get_db
from ..models import User, WecomConfig, WecomPendingMessage

logger = logging.getLogger(__name__)
router = APIRouter()


def _random_callback_path(length: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


# ---------------------------------------------------------------------------
# 从 skill 复用加解密与 XML 构造
# ---------------------------------------------------------------------------
def _get_crypt_and_helpers():
    try:
        from skills.wecom_reply.router import (
            WXBizMsgCrypt,
            _build_reply_xml,
            _parse_incoming_xml,
        )
        return WXBizMsgCrypt, _parse_incoming_xml, _build_reply_xml
    except Exception as e:
        logger.warning("[WeCom] 未加载 skill 加解密: %s", e)
        return None, None, None


# ---------------------------------------------------------------------------
# 配置 CRUD
# ---------------------------------------------------------------------------
class WecomConfigCreate(BaseModel):
    name: str = "默认应用"
    token: str
    encoding_aes_key: str
    corp_id: str = ""
    secret: Optional[str] = None  # 用于获取 access_token、发送应用消息（轮询模式）
    product_knowledge: Optional[str] = None
    """可选。与企微/本地已使用的路径一致时填此项，否则云端随机生成。"""
    callback_path: Optional[str] = None


class WecomConfigUpdate(BaseModel):
    name: Optional[str] = None
    token: Optional[str] = None
    encoding_aes_key: Optional[str] = None
    corp_id: Optional[str] = None
    secret: Optional[str] = None
    product_knowledge: Optional[str] = None


@router.get("/api/wecom/configs", summary="企业微信配置列表")
def list_wecom_configs(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.query(WecomConfig).filter(WecomConfig.user_id == current_user.id).order_by(WecomConfig.id).all()
    base = (settings.public_base_url or "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return {
        "configs": [
            {
                "id": r.id,
                "name": r.name,
                "callback_path": r.callback_path,
                "callback_url": f"{base}/api/wecom/callback/{r.callback_path}",
                "corp_id": (r.corp_id or "")[:8] + "***" if r.corp_id else "",
                "has_secret": bool((r.secret or "").strip()),
                "has_product_knowledge": bool((r.product_knowledge or "").strip()),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post("/api/wecom/configs", summary="新增企业微信配置")
def create_wecom_config(
    body: WecomConfigCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    WXBizMsgCrypt, _, _ = _get_crypt_and_helpers()
    if not WXBizMsgCrypt:
        raise HTTPException(status_code=503, detail="企业微信能力未加载（请安装 pycryptodome）")
    key = (body.encoding_aes_key or "").strip()
    if not key.endswith("="):
        key = key + "="
    try:
        WXBizMsgCrypt(body.token.strip(), key, body.corp_id or "default")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"EncodingAESKey 无效: {e}")
    path = (body.callback_path or "").strip()
    if path:
        if not all(c.isalnum() for c in path) or len(path) < 6 or len(path) > 64:
            raise HTTPException(status_code=400, detail="callback_path 需为 6～64 位字母数字")
        if db.query(WecomConfig).filter(WecomConfig.callback_path == path).first() is not None:
            raise HTTPException(status_code=400, detail="该 callback_path 已存在")
    else:
        for _ in range(5):
            path = _random_callback_path()
            if db.query(WecomConfig).filter(WecomConfig.callback_path == path).first() is None:
                break
        else:
            raise HTTPException(status_code=500, detail="生成 callback_path 冲突")
    row = WecomConfig(
        user_id=current_user.id,
        name=(body.name or "默认应用").strip() or "默认应用",
        callback_path=path,
        token=body.token.strip(),
        encoding_aes_key=key,
        corp_id=(body.corp_id or "").strip(),
        secret=(body.secret or "").strip() or None,
        product_knowledge=(body.product_knowledge or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "name": row.name,
        "callback_path": row.callback_path,
        "callback_url": f"/api/wecom/callback/{row.callback_path}",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/api/wecom/configs/{config_id:int}", summary="获取单条配置（非敏感字段，用于编辑）")
def get_wecom_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(WecomConfig).filter(WecomConfig.id == config_id, WecomConfig.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    return {
        "id": row.id,
        "name": row.name,
        "callback_path": row.callback_path,
        "corp_id": row.corp_id or "",
        "secret": row.secret or "",
        "product_knowledge": row.product_knowledge or "",
    }


@router.put("/api/wecom/configs/{config_id:int}", summary="更新企业微信配置")
def update_wecom_config(
    config_id: int,
    body: WecomConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(WecomConfig).filter(WecomConfig.id == config_id, WecomConfig.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    if body.name is not None:
        row.name = (body.name or "默认应用").strip() or "默认应用"
    if body.token is not None:
        row.token = body.token.strip()
    if body.encoding_aes_key is not None:
        key = body.encoding_aes_key.strip()
        if not key.endswith("="):
            key = key + "="
        row.encoding_aes_key = key
    if body.corp_id is not None:
        row.corp_id = (body.corp_id or "").strip()
    if body.secret is not None:
        row.secret = (body.secret or "").strip() or None
    if body.product_knowledge is not None:
        row.product_knowledge = (body.product_knowledge or "").strip() or None
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "callback_path": row.callback_path}


@router.delete("/api/wecom/configs/{config_id:int}", summary="删除企业微信配置")
def delete_wecom_config(
    config_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(WecomConfig).filter(WecomConfig.id == config_id, WecomConfig.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 回调：按 callback_path 查配置并验签、解密、回复
# ---------------------------------------------------------------------------
def _find_config_by_path(db: Session, callback_path: str) -> Optional[WecomConfig]:
    return db.query(WecomConfig).filter(WecomConfig.callback_path == callback_path).first()


@router.get("/api/wecom/callback/{callback_path:path}", summary="企业微信回调 URL 校验")
async def wecom_callback_verify(
    callback_path: str,
    request: Request,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
    db: Session = Depends(get_db),
):
    WXBizMsgCrypt, _, _ = _get_crypt_and_helpers()
    if not WXBizMsgCrypt:
        return PlainTextResponse("WECOM_NOT_CONFIGURED", status_code=503)
    cfg = _find_config_by_path(db, callback_path)
    if not cfg:
        return PlainTextResponse("config not found", status_code=404)
    # 企微文档：echostr 必须是 urldecode 后的值，再去除首尾空白
    echostr_decoded = (unquote(echostr) if echostr else "").strip()
    try:
        crypt = WXBizMsgCrypt(cfg.token, cfg.encoding_aes_key, cfg.corp_id or "default")
        if not crypt.verify_signature(msg_signature, timestamp, nonce, echostr_decoded):
            logger.warning("[WeCom] GET 验签失败 path=%s", callback_path)
            return PlainTextResponse("invalid signature", status_code=400)
        plain = crypt.decrypt(echostr_decoded)
        return PlainTextResponse(plain)
    except Exception as e:
        logger.exception("[WeCom] GET 解密失败 path=%s: %s", callback_path, e)
        return PlainTextResponse("decrypt error", status_code=400)


@router.post("/api/wecom/callback/{callback_path:path}", summary="企业微信接收消息并入队，返回占位回复")
async def wecom_callback_post(
    callback_path: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """解密后写入待处理队列，立即返回占位回复；本地轮询拉取后生成回复，再通过 submit-reply 由云端调用企微发送接口推送。"""
    WXBizMsgCrypt, _parse_incoming_xml, _build_reply_xml = _get_crypt_and_helpers()
    if not WXBizMsgCrypt or not _parse_incoming_xml or not _build_reply_xml:
        return Response(content="", status_code=503)
    cfg = _find_config_by_path(db, callback_path)
    if not cfg:
        return Response(content="", status_code=404)
    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")
    body = await request.body()
    try:
        body_str = body.decode("utf-8")
    except Exception:
        body_str = body.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(body_str)
        encrypt_el = root.find("Encrypt")
        if encrypt_el is None or not (encrypt_el.text or "").strip():
            logger.warning("[WeCom] POST 无 Encrypt")
            return Response(content="", status_code=400)
        msg_encrypt = (encrypt_el.text or "").strip()
        crypt = WXBizMsgCrypt(cfg.token, cfg.encoding_aes_key, cfg.corp_id or "default")
        if not crypt.verify_signature(msg_signature, timestamp, nonce, msg_encrypt):
            logger.warning("[WeCom] POST 验签失败 path=%s", callback_path)
            return Response(content="", status_code=400)
        msg_xml = crypt.decrypt(msg_encrypt)
        parsed = _parse_incoming_xml(msg_xml)
        msg_type = (parsed.get("MsgType") or "").strip().lower()
        from_user = (parsed.get("FromUserName") or "").strip()
        to_user = (parsed.get("ToUserName") or "").strip()
        content = (parsed.get("Content") or "").strip()
        agent_id_raw = (parsed.get("AgentID") or parsed.get("AgentId") or "").strip()
        try:
            agent_id = int(agent_id_raw) if agent_id_raw else None
        except ValueError:
            agent_id = None
        # 入队，供本地轮询拉取
        pending = WecomPendingMessage(
            wecom_config_id=cfg.id,
            from_user=from_user,
            to_user=to_user,
            agent_id=agent_id,
            content=content,
            msg_type=msg_type or "text",
            status="pending",
        )
        db.add(pending)
        db.commit()
        # 不主动回复，仅入队；用户通过「拉取并回复」由 AI 生成并发送应用消息
        reply_xml = _build_reply_xml(from_user, to_user, "")
        reply_encrypt = crypt.encrypt(reply_xml)
        reply_nonce = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        reply_ts = str(int(time.time()))
        reply_sig = crypt._signature(cfg.token, reply_ts, reply_nonce, reply_encrypt)
        resp_xml = (
            "<xml>"
            "<Encrypt><![CDATA[{}]]></Encrypt>"
            "<MsgSignature><![CDATA[{}]]></MsgSignature>"
            "<TimeStamp>{}</TimeStamp>"
            "<Nonce><![CDATA[{}]]></Nonce>"
            "</xml>"
        ).format(reply_encrypt, reply_sig, reply_ts, reply_nonce)
        return Response(content=resp_xml, media_type="application/xml", status_code=200)
    except ET.ParseError as e:
        logger.warning("[WeCom] POST XML 解析失败: %s", e)
        return Response(content="", status_code=400)
    except Exception as e:
        logger.exception("[WeCom] POST 处理异常: %s", e)
        return Response(content="", status_code=500)


def _check_forward_secret(x_forward_secret: Optional[str] = Header(None, alias="X-Forward-Secret")):
    secret = (settings.wecom_forward_secret or "").strip()
    if secret and x_forward_secret != secret:
        raise HTTPException(status_code=401, detail="X-Forward-Secret invalid")
    return True


class SubmitReplyBody(BaseModel):
    message_id: int
    reply_text: str


@router.get("/api/wecom/pending", summary="本地轮询：拉取待处理消息")
def wecom_pending(
    callback_path: Optional[str] = None,
    limit: int = 20,
    _auth: bool = Depends(_check_forward_secret),
    db: Session = Depends(get_db),
):
    logger.info("[WeCom] GET pending callback_path=%s limit=%s", callback_path, limit)
    q = (
        db.query(WecomPendingMessage, WecomConfig.callback_path)
        .join(WecomConfig, WecomPendingMessage.wecom_config_id == WecomConfig.id)
        .filter(WecomPendingMessage.status == "pending")
        .order_by(WecomPendingMessage.id.asc())
    )
    if callback_path:
        q = q.filter(WecomConfig.callback_path == callback_path)
    rows = q.limit(max(1, min(limit, 100))).all()
    return {
        "items": [
            {
                "id": r.id,
                "callback_path": path or "",
                "from_user": r.from_user,
                "to_user": r.to_user,
                "content": r.content,
                "msg_type": r.msg_type,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r, path in rows
        ],
    }


@router.post("/api/wecom/submit-reply", summary="本地提交回复，云端代发企微应用消息")
async def wecom_submit_reply(
    body: SubmitReplyBody,
    _auth: bool = Depends(_check_forward_secret),
    db: Session = Depends(get_db),
):
    logger.info("[WeCom] POST submit-reply message_id=%s", body.message_id)
    row = db.query(WecomPendingMessage).filter(
        WecomPendingMessage.id == body.message_id,
        WecomPendingMessage.status == "pending",
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="消息不存在或已处理")
    cfg = db.query(WecomConfig).filter(WecomConfig.id == row.wecom_config_id).first()
    if not cfg or not (cfg.corp_id or "").strip() or not (cfg.secret or "").strip():
        row.status = "failed"
        row.reply_text = (body.reply_text or "")[:500]
        db.commit()
        raise HTTPException(status_code=400, detail="该应用未配置 corp_id 或 secret，无法发送应用消息")
    # 获取 access_token
    async with httpx.AsyncClient() as client:
        token_url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        r = await client.get(token_url, params={"corpid": cfg.corp_id, "corpsecret": cfg.secret})
        r.raise_for_status()
        data = r.json()
    if data.get("errcode") != 0:
        row.status = "failed"
        db.commit()
        raise HTTPException(status_code=502, detail=f"企微 gettoken 失败: {data.get('errmsg', '')}")
    access_token = data.get("access_token", "")
    # 发送应用消息：touser=接收者 userid（发消息的人），agentid=应用 ID（数字）
    agentid = getattr(row, "agent_id", None) or getattr(cfg, "agent_id", None)
    if agentid is None:
        try:
            agentid = int(row.to_user) if (row.to_user or "").strip().isdigit() else None
        except (ValueError, TypeError):
            agentid = None
    if agentid is None:
        row.status = "failed"
        row.reply_text = (body.reply_text or "")[:500]
        db.commit()
        raise HTTPException(
            status_code=400,
            detail="缺少应用 AgentId（应用 ID）。请在企微后台应用详情中查看并配置 agent_id，或确保回调消息体内含 AgentID",
        )
    send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
    payload = {
        "touser": row.from_user,
        "msgtype": "text",
        "agentid": int(agentid) if not isinstance(agentid, int) else agentid,
        "text": {"content": (body.reply_text or "").strip() or "收到。"},
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(send_url, json=payload)
        r.raise_for_status()
        data = r.json()
    if data.get("errcode") != 0:
        row.status = "failed"
        row.reply_text = (body.reply_text or "")[:500]
        db.commit()
        raise HTTPException(status_code=502, detail=f"企微发送消息失败: {data.get('errmsg', '')}")
    row.status = "replied"
    row.reply_text = (body.reply_text or "").strip()[:2000]
    db.commit()
    return {"ok": True}
