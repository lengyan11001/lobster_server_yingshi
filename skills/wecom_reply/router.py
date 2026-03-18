"""企业微信回调：接收消息 → AI 回复 → 被动回复。"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import string
import time
import xml.etree.ElementTree as ET

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_reply_for_channel():
    """延迟导入，避免 skills 在未挂载 backend 时导入失败。"""
    from backend.app.api.chat import get_reply_for_channel as _fn
    return _fn


# 从环境变量读取，与 .env 一致
def _get_wecom_token() -> str:
    return (os.environ.get("WECOM_TOKEN") or "").strip()


def _get_wecom_aes_key() -> str:
    return (os.environ.get("WECOM_AES_KEY") or os.environ.get("WECOM_ENCODING_AES_KEY") or "").strip()


def _get_wecom_corp_id() -> str:
    return (os.environ.get("WECOM_CORP_ID") or "").strip()


def _get_product_knowledge_extra() -> str:
    """可选：从环境变量指定文件或默认产品列表，注入 AI 回复。"""
    path = (os.environ.get("WECOM_PRODUCT_KNOWLEDGE_PATH") or "").strip()
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            logger.warning("[WeCom] 读取产品知识文件失败: %s", e)
    return (
        "当前产品线：A3（星空灰）、A5.31、D11（200/215）、KA10（灰色/白色）、"
        "q2-1304d10xfhy（太空灰）。可根据用户询问简要介绍。"
    )


def _wecom_enabled() -> bool:
    return bool(_get_wecom_token() and _get_wecom_aes_key())


class WXBizMsgCrypt:
    """企业微信消息加解密（AES-256-CBC，IV=key[:16]）。"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        key_b64 = (encoding_aes_key or "").strip()
        # 企微 EncodingAESKey 为 43 位，base64 解码需补足到 4 的倍数
        while len(key_b64) % 4:
            key_b64 += "="
        self.aes_key = self._b64decode(key_b64)
        if len(self.aes_key) != 32:
            raise ValueError("EncodingAESKey 解码后应为 32 字节")

    @staticmethod
    def _b64decode(s: str) -> bytes:
        import base64
        return base64.b64decode(s)

    @staticmethod
    def _b64encode(b: bytes) -> str:
        import base64
        return base64.b64encode(b).decode("ascii")

    def _signature(self, *parts: str) -> str:
        s = "".join(sorted(parts))
        return hashlib.sha1(s.encode("utf-8")).hexdigest()

    def verify_signature(self, msg_signature: str, timestamp: str, nonce: str, msg_encrypt: str) -> bool:
        return self._signature(self.token, timestamp, nonce, msg_encrypt) == msg_signature

    def decrypt(self, msg_encrypt_b64: str) -> str:
        # 去除全部空白（含换行），避免长密文被截断或解码错
        b64 = "".join((msg_encrypt_b64 or "").split())
        b64 = b64.strip()
        while len(b64) % 4:
            b64 += "="
        aes_msg = self._b64decode(b64)
        # 定位解密失败：密文长度需为 16 的倍数，否则解密结果无效
        logger.info(
            "[WeCom] decrypt 密文长度 raw=%d b64_after=%d cipher_bytes=%d cipher_mod16=%d",
            len(msg_encrypt_b64 or ""), len(b64), len(aes_msg), len(aes_msg) % 16,
        )
        iv = self.aes_key[:16]
        cipher = AES.new(self.aes_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(aes_msg)
        # 用最后一字节表示 padding 长度并切除；若为 0 或 >16 则视为明文已块对齐、无 PKCS7（最后一字节为内容）
        if len(decrypted) < 16 + 4:
            raise ValueError("解密后长度不足")
        pad_len = decrypted[-1]
        if 1 <= pad_len <= 16:
            rand_msg = decrypted[:-pad_len]
        else:
            rand_msg = decrypted
        if len(rand_msg) < 16 + 4:
            raise ValueError("解密后长度不足")
        msg_len = int.from_bytes(rand_msg[16:20], "big")
        if 20 + msg_len > len(rand_msg):
            raise ValueError("msg_len 越界")
        msg = rand_msg[20 : 20 + msg_len].decode("utf-8")
        return msg

    def encrypt(self, msg: str) -> str:
        rand_16 = os.urandom(16)
        msg_bytes = msg.encode("utf-8")
        msg_len = len(msg_bytes).to_bytes(4, "big")
        plain = rand_16 + msg_len + msg_bytes + self.corp_id.encode("utf-8")
        padded = pad(plain, AES.block_size)
        iv = self.aes_key[:16]
        cipher = AES.new(self.aes_key, AES.MODE_CBC, iv)
        enc = cipher.encrypt(padded)
        return self._b64encode(enc)


def _parse_incoming_xml(xml_str: str) -> dict:
    root = ET.fromstring(xml_str)
    out = {}
    for child in root:
        if child.text:
            out[child.tag] = child.text.strip()
        else:
            out[child.tag] = (child.text or "").strip()
    return out


def _build_reply_xml(to_user: str, from_user: str, content: str) -> str:
    t = str(int(time.time()))
    return (
        "<xml>"
        "<ToUserName><![CDATA[{}]]></ToUserName>"
        "<FromUserName><![CDATA[{}]]></FromUserName>"
        "<CreateTime>{}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[{}]]></Content>"
        "</xml>"
    ).format(to_user, from_user, t, content)


@router.get("/api/wecom/callback", summary="企业微信回调 URL 校验")
async def wecom_callback_verify(
    request: Request,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
):
    """企业微信后台配置回调 URL 时发起 GET，需验签并返回解密后的 echostr。"""
    if not _wecom_enabled():
        return PlainTextResponse("WECOM_NOT_CONFIGURED", status_code=503)
    token = _get_wecom_token()
    aes_key = _get_wecom_aes_key()
    corp_id = _get_wecom_corp_id()
    if not corp_id:
        corp_id = "default"
    try:
        crypt = WXBizMsgCrypt(token, aes_key, corp_id)
        if not crypt.verify_signature(msg_signature, timestamp, nonce, echostr):
            logger.warning("[WeCom] GET 验签失败")
            return PlainTextResponse("invalid signature", status_code=400)
        plain = crypt.decrypt(echostr)
        return PlainTextResponse(plain)
    except Exception as e:
        logger.exception("[WeCom] GET 解密失败: %s", e)
        return PlainTextResponse("decrypt error", status_code=400)


@router.post("/api/wecom/callback", summary="企业微信接收消息并自动回复")
async def wecom_callback_post(request: Request):
    """接收企业微信推送的加密消息，解密后调 AI 生成回复，5 秒内返回被动回复 XML。"""
    if not _wecom_enabled():
        return Response(content="", status_code=503)
    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")
    body = await request.body()
    try:
        body_str = body.decode("utf-8")
    except Exception:
        body_str = body.decode("utf-8", errors="replace")
    token = _get_wecom_token()
    aes_key = _get_wecom_aes_key()
    corp_id = _get_wecom_corp_id()
    if not corp_id:
        corp_id = "default"
    try:
        root = ET.fromstring(body_str)
        encrypt_el = root.find("Encrypt")
        if encrypt_el is None or not (encrypt_el.text or "").strip():
            logger.warning("[WeCom] POST 无 Encrypt")
            return Response(content="", status_code=400)
        msg_encrypt = (encrypt_el.text or "").strip()
        crypt = WXBizMsgCrypt(token, aes_key, corp_id)
        if not crypt.verify_signature(msg_signature, timestamp, nonce, msg_encrypt):
            logger.warning("[WeCom] POST 验签失败")
            return Response(content="", status_code=400)
        msg_xml = crypt.decrypt(msg_encrypt)
        parsed = _parse_incoming_xml(msg_xml)
        msg_type = (parsed.get("MsgType") or "").strip().lower()
        from_user = (parsed.get("FromUserName") or "").strip()
        to_user = (parsed.get("ToUserName") or "").strip()
        content = (parsed.get("Content") or "").strip()
        if msg_type != "text":
            reply_text = "当前仅支持文字消息，请发送文字。"
        else:
            session_id = f"wecom_{from_user}"
            product_extra = _get_product_knowledge_extra()
            if product_extra:
                product_extra = "\n【产品信息】\n" + product_extra
            get_reply_for_channel = _get_reply_for_channel()
            reply_text = await get_reply_for_channel(
                content, session_id=session_id, system_prompt_extra=product_extra
            )
        reply_xml = _build_reply_xml(from_user, to_user, reply_text)
        reply_encrypt = crypt.encrypt(reply_xml)
        reply_nonce = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        reply_ts = str(int(time.time()))
        reply_sig = crypt._signature(token, reply_ts, reply_nonce, reply_encrypt)
        resp_xml = (
            "<xml>"
            "<Encrypt><![CDATA[{}]]></Encrypt>"
            "<MsgSignature><![CDATA[{}]]></MsgSignature>"
            "<TimeStamp>{}</TimeStamp>"
            "<Nonce><![CDATA[{}]]></Nonce>"
            "</xml>"
        ).format(reply_encrypt, reply_sig, reply_ts, reply_nonce)
        return Response(
            content=resp_xml,
            media_type="application/xml",
            status_code=200,
        )
    except ET.ParseError as e:
        logger.warning("[WeCom] POST XML 解析失败: %s", e)
        return Response(content="", status_code=400)
    except Exception as e:
        logger.exception("[WeCom] POST 处理异常: %s", e)
        return Response(content="", status_code=500)
