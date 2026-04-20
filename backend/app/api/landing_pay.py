"""官网落地页（INSclaw）匿名购买 + 微信支付 + 下载凭证。

设计要点：
- 不依赖登录态：访客在公网落地页直接扫码付款。
- 复用现有微信支付 (wechatpayv3 Native) 的密钥/证书配置（与 /api/recharge/wechat-* 同源）。
- 订单走独立表 LandingOrder（无 user_id），与积分充值 RechargeOrder 隔离。
- 支付完成后生成 download_token（UUID4），有效期 7 天，下载次数 ≤ 10。
- 安装包 zip 放 landing/_private/（不通过 StaticFiles 公开），仅本接口 FileResponse 转发。
- 防刷：同 IP 5 分钟内最多 3 次 create-order。

接口：
- GET  /api/landing/products            可购买产品（默认单品 99 元）
- POST /api/landing/create-order        创建订单 + 调微信 Native 下单 → code_url + qr_png
- GET  /api/landing/order-status        公开查询订单状态；未支付时主动调微信查单
- GET  /api/landing/download            凭 download_token 下载安装包
- POST /api/landing/wechat-notify       微信支付异步回调（验签 + 入账 + 生成 token）
- GET  /api/landing/qr-png              将字符串编码为 PNG 二维码（避免外链图床）
"""
from __future__ import annotations

import io
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import settings, get_effective_public_base_url
from ..db import get_db
from ..models import LandingOrder

logger = logging.getLogger(__name__)
router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
# 私有安装包目录：必须放在 landing/ 之外，避免被 /landing StaticFiles 公开暴露。
# 仅本接口的 download_token 校验通过后才会通过 FileResponse 转发。
_PRIVATE_DIR = _BASE_DIR / "landing_private"
_PRIVATE_DIR.mkdir(parents=True, exist_ok=True)

# ── 产品配置：单品 99 元 INSclaw（购买后可下载 完整 + 轻量 两个版本）─────
# 文件放在 _PRIVATE_DIR 下；只要把对应的 .zip 放到该目录即可。
PRODUCTS: Dict[str, Dict[str, Any]] = {
    "insclaw_full": {
        "name": "INSclaw · Windows 安装包",
        "price_fen": 9900,  # ¥99
        "description": "购买后即可下载完整版（含 Chromium / Node / Python 运行时，约 3GB，开箱即用）或轻量版（约 220MB，本机有 Python/Node 时联网装依赖）。",
    },
}

# 一笔订单可下载两种安装包（完整 / 轻量），对应 _PRIVATE_DIR 下的实际 zip 文件名
INSCLAW_FILE_VARIANTS: Dict[str, Dict[str, str]] = {
    "full": {
        "filename": "INSclaw-Setup-Windows-x64.zip",
        "download_filename": "INSclaw-Setup-Windows-x64.zip",
        "label": "完整版（约 3GB · 含运行时·开箱即用）",
    },
    "slim": {
        "filename": "INSclaw-Slim-Windows-x64.zip",
        "download_filename": "INSclaw-Slim-Windows-x64.zip",
        "label": "轻量版（约 220MB · 联网装依赖·适合开发者）",
    },
}

# ── 防刷：同 IP 时窗内 create-order 次数（进程内简易计数器） ──────────────
_RATE_LIMIT_WINDOW_SEC = 300  # 5 分钟
_RATE_LIMIT_MAX = 3
_rate_buckets: Dict[str, List[float]] = {}


def _client_ip(request: Request) -> str:
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if fwd:
        return fwd[:64]
    try:
        return (request.client.host if request.client else "")[:64] or "unknown"
    except Exception:
        return "unknown"


def _check_rate_limit(ip: str) -> bool:
    """返回 True 表示通过；False 表示超限。"""
    now = time.time()
    bucket = _rate_buckets.setdefault(ip, [])
    cutoff = now - _RATE_LIMIT_WINDOW_SEC
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= _RATE_LIMIT_MAX:
        return False
    bucket.append(now)
    return True


# ── 复用 wechat 配置 ─────────────────────────────────────────────────────


def _wechat_pay_configured() -> bool:
    mch_id = (getattr(settings, "wechat_mch_id", None) or "").strip()
    key = (getattr(settings, "wechat_pay_apiv3_key", None) or "").strip()
    serial = (getattr(settings, "wechat_pay_serial_no", None) or "").strip()
    key_path = (getattr(settings, "wechat_pay_private_key_path", None) or "").strip()
    app_id = (getattr(settings, "wechat_app_id", None) or "").strip()
    return bool(mch_id and key and serial and key_path and app_id)


def _make_wxpay() -> Any:
    """实例化 wechatpayv3.WeChatPay（NATIVE 类型）。出现配置缺失/读取失败抛 HTTPException。"""
    from wechatpayv3 import WeChatPay, WeChatPayType

    key_path = Path((getattr(settings, "wechat_pay_private_key_path", None) or "").strip())
    if not key_path.is_absolute():
        key_path = _BASE_DIR / key_path
    try:
        private_key = key_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("wechat pay private key read failed: %s", e)
        raise HTTPException(status_code=500, detail="微信支付商户私钥读取失败")

    apiv3_key = (getattr(settings, "wechat_pay_apiv3_key", None) or "").strip()[:32]

    cert_dir_raw = (getattr(settings, "wechat_pay_cert_dir", None) or "").strip()
    cert_dir: Optional[Path] = None
    if cert_dir_raw:
        cert_dir = Path(cert_dir_raw)
        if not cert_dir.is_absolute():
            cert_dir = _BASE_DIR / cert_dir
        cert_dir.mkdir(parents=True, exist_ok=True)

    public_key_path_raw = (getattr(settings, "wechat_pay_public_key_path", None) or "").strip()
    public_key_id = (getattr(settings, "wechat_pay_public_key_id", None) or "").strip()
    public_key_content: Optional[str] = None
    if public_key_path_raw and public_key_id:
        pub_path = Path(public_key_path_raw)
        if not pub_path.is_absolute():
            pub_path = _BASE_DIR / pub_path
        try:
            public_key_content = pub_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning("wechat pay public key read failed: %s", e)

    kwargs = dict(
        wechatpay_type=WeChatPayType.NATIVE,
        mchid=(getattr(settings, "wechat_mch_id", None) or "").strip(),
        private_key=private_key,
        cert_serial_no=(getattr(settings, "wechat_pay_serial_no", None) or "").strip(),
        apiv3_key=apiv3_key,
        appid=(getattr(settings, "wechat_app_id", None) or "").strip(),
    )
    if cert_dir is not None:
        kwargs["cert_dir"] = str(cert_dir)
    if public_key_content and public_key_id:
        kwargs["public_key"] = public_key_content
        kwargs["public_key_id"] = public_key_id
    return WeChatPay(**kwargs)


# ── 模型/请求体 ──────────────────────────────────────────────────────────


class CreateOrderBody(BaseModel):
    product_id: str
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None


# ── 接口实现 ─────────────────────────────────────────────────────────────


@router.get("/api/landing/products", summary="可购买产品列表（公开，无需登录）")
def landing_products():
    out = []
    for pid, p in PRODUCTS.items():
        out.append({
            "id": pid,
            "name": p["name"],
            "price_yuan": round(p["price_fen"] / 100, 2),
            "price_fen": p["price_fen"],
            "description": p.get("description", ""),
        })
    return {"products": out, "wechat_pay_configured": _wechat_pay_configured()}


@router.post("/api/landing/create-order", summary="创建落地页购买订单 + 微信 Native 下单")
def landing_create_order(body: CreateOrderBody, request: Request, db: Session = Depends(get_db)):
    if not _wechat_pay_configured():
        raise HTTPException(status_code=503, detail="支付暂未开通，请稍后再试或联系客服")
    product = PRODUCTS.get(body.product_id)
    if not product:
        raise HTTPException(status_code=400, detail=f"未知商品: {body.product_id}")

    ip = _client_ip(request)
    if not _check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    out_trade_no = f"L_{int(time.time())}_{uuid.uuid4().hex[:10]}"
    order = LandingOrder(
        out_trade_no=out_trade_no,
        product_id=body.product_id,
        amount_fen=int(product["price_fen"]),
        status="pending",
        payment_method="wechat",
        contact_email=(body.contact_email or "").strip()[:128] or None,
        contact_phone=(body.contact_phone or "").strip()[:32] or None,
        client_ip=ip,
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    base_url = get_effective_public_base_url()
    notify_url = f"{base_url}/api/landing/wechat-notify"
    try:
        wxpay = _make_wxpay()
        first, second = wxpay.pay(
            description=f"INSclaw-{product['name']}",
            out_trade_no=out_trade_no,
            amount={"total": int(product["price_fen"])},
            notify_url=notify_url,
        )
        code_url = ""
        if isinstance(first, str) and first.strip().startswith("weixin://"):
            code_url = first.strip()
        elif first == 200 or first == "200":
            resp = second
            if isinstance(resp, str):
                try:
                    resp = json.loads(resp)
                except Exception:
                    resp = {}
            code_url = (resp.get("code_url") if isinstance(resp, dict) else None) or ""
            code_url = str(code_url).strip()
        if not code_url:
            logger.warning("landing wechat pay first=%s second=%s", first, second)
            raise HTTPException(status_code=502, detail="微信下单返回异常，请稍后重试")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("landing wechat pay create failed: %s", e)
        raise HTTPException(status_code=502, detail="微信下单失败，请稍后重试")

    return {
        "out_trade_no": out_trade_no,
        "amount_fen": int(product["price_fen"]),
        "amount_yuan": round(product["price_fen"] / 100, 2),
        "product_id": body.product_id,
        "product_name": product["name"],
        "code_url": code_url,
        "qr_png_url": f"/api/landing/qr-png?data={code_url}",
        "status": "pending",
    }


@router.get("/api/landing/order-status", summary="公开查询订单状态；未支付时主动调微信查单")
def landing_order_status(out_trade_no: str = Query(..., min_length=8, max_length=64), db: Session = Depends(get_db)):
    out_trade_no = (out_trade_no or "").strip()
    order = db.query(LandingOrder).filter(LandingOrder.out_trade_no == out_trade_no).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    if order.status == "paid":
        return _order_to_paid_response(order)

    # 未支付：主动调微信查单（即使没收到回调也能确认到账）
    if not _wechat_pay_configured():
        return {"status": order.status, "out_trade_no": out_trade_no}
    try:
        wxpay = _make_wxpay()
        mchid = (getattr(settings, "wechat_mch_id", None) or "").strip()
        try:
            result = wxpay.query_order(out_trade_no=out_trade_no, mchid=mchid)
        except AttributeError:
            result = wxpay.query(out_trade_no=out_trade_no, mchid=mchid)
    except Exception as e:
        logger.warning("landing query order failed: %s", e)
        return {"status": order.status, "out_trade_no": out_trade_no}

    if isinstance(result, (list, tuple)) and len(result) >= 2:
        code, body = result[0], result[1]
        if code != 200:
            return {"status": order.status, "out_trade_no": out_trade_no}
        resp = body if isinstance(body, dict) else (json.loads(body) if isinstance(body, str) else {})
    else:
        resp = result if isinstance(result, dict) else {}

    trade_state = (resp.get("trade_state") or "").strip()
    if trade_state != "SUCCESS":
        return {"status": order.status, "out_trade_no": out_trade_no, "trade_state": trade_state}

    amount_info = resp.get("amount") if isinstance(resp.get("amount"), dict) else None
    paid_fen = int(amount_info.get("total")) if amount_info and amount_info.get("total") is not None else None
    if paid_fen is None or paid_fen != order.amount_fen:
        logger.error(
            "landing query amount_mismatch out_trade_no=%s expected=%s paid=%s",
            out_trade_no, order.amount_fen, paid_fen,
        )
        return {"status": order.status, "out_trade_no": out_trade_no, "error": "金额异常，请联系客服"}
    transaction_id = (resp.get("transaction_id") or "").strip() or None
    _mark_order_paid(order, paid_fen, transaction_id, db, channel_label="微信支付（主动查单）")
    return _order_to_paid_response(order)


@router.post("/api/landing/wechat-notify", summary="微信支付异步回调（落地页订单专用）")
async def landing_wechat_notify(request: Request, db: Session = Depends(get_db)):
    if not _wechat_pay_configured():
        return PlainTextResponse("fail", status_code=500)
    raw = await request.body()
    headers = dict(request.headers)
    try:
        wxpay = _make_wxpay()
        result = wxpay.callback(headers, raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception as e:
        logger.warning("landing_notify callback verify failed: %s (type=%s)", e, type(e).__name__)
        return PlainTextResponse("fail", status_code=400)
    event_type = result.get("event_type") if isinstance(result, dict) else None
    if not result or event_type != "TRANSACTION.SUCCESS":
        return PlainTextResponse("success")
    res = result.get("resource") if isinstance(result.get("resource"), dict) else result
    payload = res if isinstance(res, dict) else result
    out_trade_no = (payload.get("out_trade_no") or result.get("out_trade_no") or "").strip()
    if not out_trade_no:
        return PlainTextResponse("success")
    order = db.query(LandingOrder).filter(LandingOrder.out_trade_no == out_trade_no).first()
    if not order:
        logger.warning("landing_notify order not found out_trade_no=%s", out_trade_no)
        return PlainTextResponse("success")
    if order.status == "paid":
        return PlainTextResponse("success")
    amount_info = payload.get("amount") if isinstance(payload.get("amount"), dict) else None
    paid_fen = int(amount_info.get("total")) if amount_info and amount_info.get("total") is not None else None
    if paid_fen is None or paid_fen != order.amount_fen:
        logger.error(
            "landing_notify amount_mismatch out_trade_no=%s expected=%s paid=%s",
            out_trade_no, order.amount_fen, paid_fen,
        )
        return PlainTextResponse("fail", status_code=400)
    transaction_id = (payload.get("transaction_id") or result.get("transaction_id") or "").strip() or None
    _mark_order_paid(order, paid_fen, transaction_id, db, channel_label="微信支付（回调）")
    logger.info("landing_notify success order_id=%s out_trade_no=%s", order.id, out_trade_no)
    return PlainTextResponse("success")


@router.get("/api/landing/download", summary="凭 download_token 下载安装包（kind=full|slim）")
def landing_download(
    token: str = Query(..., min_length=8, max_length=64),
    kind: str = Query("full", pattern="^(full|slim)$"),
    db: Session = Depends(get_db),
):
    token = (token or "").strip()
    order = db.query(LandingOrder).filter(LandingOrder.download_token == token).first()
    if not order:
        raise HTTPException(status_code=404, detail="下载凭证无效")
    if order.status != "paid":
        raise HTTPException(status_code=403, detail="订单未支付")
    now = datetime.utcnow()
    if order.download_token_expires_at and now > order.download_token_expires_at:
        raise HTTPException(status_code=410, detail="下载凭证已过期，请联系客服")
    if order.download_count >= 20:
        raise HTTPException(status_code=429, detail="下载次数已达上限，请联系客服")

    variant = INSCLAW_FILE_VARIANTS.get(kind)
    if not variant:
        raise HTTPException(status_code=400, detail=f"未知的下载类型: {kind}")
    file_path = _PRIVATE_DIR / variant["filename"]
    if not file_path.is_file():
        logger.error("landing download missing file: %s (order=%s, kind=%s)", file_path, order.out_trade_no, kind)
        raise HTTPException(
            status_code=503,
            detail=f"{variant['label']} 暂未就绪，请联系客服或换另一个版本",
        )
    order.download_count = (order.download_count or 0) + 1
    db.commit()
    return FileResponse(
        path=str(file_path),
        media_type="application/zip",
        filename=variant["download_filename"],
    )


@router.get("/api/landing/qr-png", summary="将字符串转为 PNG 二维码（用于扫码支付）")
def landing_qr_png(data: str = Query(..., min_length=1, max_length=4096)):
    """复用 /api/recharge/qr-png 一样的纯本地 segno 渲染，避免外链图床。"""
    try:
        import segno
    except Exception:
        raise HTTPException(status_code=500, detail="服务器未安装 segno（pip install segno）")
    try:
        qr = segno.make(data, micro=False)
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=8, dark="black", light="white")
        return Response(content=buf.getvalue(), media_type="image/png")
    except Exception as e:
        logger.warning("landing qr png render failed: %s", e)
        raise HTTPException(status_code=500, detail="二维码生成失败")


# ── 内部 helper ──────────────────────────────────────────────────────────


def _mark_order_paid(
    order: LandingOrder,
    paid_fen: int,
    transaction_id: Optional[str],
    db: Session,
    *,
    channel_label: str = "微信支付",
) -> None:
    order.callback_amount_fen = paid_fen
    order.wechat_transaction_id = transaction_id
    order.status = "paid"
    order.paid_at = datetime.utcnow()
    if not order.download_token:
        order.download_token = uuid.uuid4().hex
        order.download_token_expires_at = datetime.utcnow() + timedelta(days=7)
    db.commit()
    db.refresh(order)
    logger.info(
        "landing order paid (%s) order_id=%s out_trade_no=%s product=%s amount_fen=%s txn=%s",
        channel_label, order.id, order.out_trade_no, order.product_id, paid_fen, transaction_id,
    )


def _order_to_paid_response(order: LandingOrder) -> Dict[str, Any]:
    downloads = []
    if order.download_token:
        for kind, variant in INSCLAW_FILE_VARIANTS.items():
            downloads.append({
                "kind": kind,
                "label": variant["label"],
                "url": f"/api/landing/download?token={order.download_token}&kind={kind}",
                "filename": variant["download_filename"],
            })
    return {
        "status": "paid",
        "out_trade_no": order.out_trade_no,
        "product_id": order.product_id,
        "amount_fen": order.amount_fen,
        "download_token": order.download_token,
        "downloads": downloads,
        # 兼容旧前端：默认指向 full 版
        "download_url": f"/api/landing/download?token={order.download_token}&kind=full" if order.download_token else None,
        "download_expires_at": order.download_token_expires_at.isoformat() if order.download_token_expires_at else None,
    }
