"""微信服务号：服务器配置（GET 验证 + POST 接收消息）。消息推送到此地址。"""
import hashlib
import logging
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from ..core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_token() -> str:
    return (getattr(settings, "wechat_oa_token", None) or "").strip()


@router.get("/api/wechat", summary="服务号服务器配置：微信 GET 验证")
def wechat_verify(
    signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
):
    """微信服务器验证：token+timestamp+nonce 字典序排序后拼接做 SHA1，与 signature 一致则原样返回 echostr。"""
    token = _get_token()
    if not token:
        return PlainTextResponse("", status_code=403)
    lst = sorted([token, timestamp, nonce])
    s = "".join(lst)
    h = hashlib.sha1(s.encode("utf-8")).hexdigest()
    if h != signature:
        return PlainTextResponse("", status_code=403)
    return PlainTextResponse(echostr)


@router.post("/api/wechat", summary="服务号服务器配置：接收消息（明文模式）")
async def wechat_message(request: Request):
    """明文模式：body 为 XML，解析后打日志；返回空 200 避免微信报错。"""
    token = _get_token()
    if not token:
        return Response(status_code=403)
    try:
        body = await request.body()
        if body:
            root = ET.fromstring(body.decode("utf-8"))
            msg_type = root.find("MsgType")
            from_user = root.find("FromUserName")
            to_user = root.find("ToUserName")
            content = root.find("Content")
            msg_type_text = msg_type.text if msg_type is not None else ""
            from_text = from_user.text if from_user is not None else ""
            to_text = to_user.text if to_user is not None else ""
            content_text = content.text if content is not None else ""
            logger.info("[微信服务号] 收到消息 type=%s From=%s To=%s Content=%s", msg_type_text, from_text, to_text, content_text[:80] if content_text else "")
    except Exception as e:
        logger.warning("[微信服务号] 解析 POST body 失败: %s", e)
    return Response(status_code=200, content="", media_type="text/plain")
