"""软件收费模式配置与展示：技能解锁价格、算力套餐（积分兑换比例）；自有充值订单；自建微信支付。"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from io import BytesIO

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import settings, get_effective_public_base_url
from ..db import get_db
from .auth import get_current_user
from ..models import CapabilityCallLog, CreditLedger, RechargeOrder, User
from ..services.credit_ledger import append_credit_ledger, public_ledger_meta
from ..services.credits_amount import (
    credits_json_float,
    credits_json_float_signed,
    ledger_display_delta,
    quantize_credits,
)

logger = logging.getLogger(__name__)

_BJ = ZoneInfo("Asia/Shanghai")


def _dt_utc_naive_to_beijing_str(dt: Optional[datetime]) -> str:
    """库内时间按 UTC naive 存储时，转为北京时间展示字符串。"""
    if not dt:
        return ""
    u = dt
    if u.tzinfo is not None:
        u = u.astimezone(timezone.utc).replace(tzinfo=None)
    aware = u.replace(tzinfo=timezone.utc)
    return aware.astimezone(_BJ).strftime("%Y-%m-%d %H:%M:%S")


def _wechat_pay_configured() -> bool:
    mch_id = (getattr(settings, "wechat_mch_id", None) or "").strip()
    key = (getattr(settings, "wechat_pay_apiv3_key", None) or "").strip()
    serial = (getattr(settings, "wechat_pay_serial_no", None) or "").strip()
    key_path = (getattr(settings, "wechat_pay_private_key_path", None) or "").strip()
    app_id = (getattr(settings, "wechat_app_id", None) or "").strip()
    return bool(mch_id and key and serial and key_path and app_id)


def _get_public_base_url() -> str:
    """微信支付回调等用。未配置 PUBLIC_BASE_URL 时用本机 IP:PORT。"""
    return get_effective_public_base_url()


def _apply_wechat_paid_to_order(
    order: RechargeOrder,
    paid_total_fen: int,
    wechat_transaction_id: Optional[str],
    db: Session,
) -> bool:
    """校验金额一致后写入审计、加积分、置已支付。返回 True 表示已处理，False 表示金额不符未处理。"""
    from datetime import datetime
    expected_fen = (order.amount_fen or 0) or (order.amount_yuan * 100)
    if paid_total_fen != expected_fen:
        logger.error(
            "wechat paid amount_mismatch out_trade_no=%s order_id=%s paid_fen=%s expected_fen=%s",
            order.out_trade_no, order.id, paid_total_fen, expected_fen,
        )
        return False
    order.callback_amount_fen = paid_total_fen
    order.wechat_transaction_id = wechat_transaction_id
    user = db.query(User).filter(User.id == order.user_id).first()
    if user:
        add_c = quantize_credits(order.credits or 0)
        user.credits = quantize_credits(user.credits or 0) + add_c
        bal = quantize_credits(user.credits)
        append_credit_ledger(
            db,
            user.id,
            add_c,
            "recharge",
            bal,
            description="充值到账（微信支付）",
            ref_type="recharge_order",
            ref_id=(order.out_trade_no or "")[:128],
            meta={
                "order_id": order.id,
                "amount_fen": paid_total_fen,
                "wechat_transaction_id": wechat_transaction_id,
            },
        )
    order.status = "paid"
    order.paid_at = datetime.utcnow()
    db.commit()
    logger.info(
        "wechat paid (query or notify) order_id=%s out_trade_no=%s paid_fen=%s credits_granted=%s user_id=%s transaction_id=%s",
        order.id, order.out_trade_no, paid_total_fen, order.credits, order.user_id, wechat_transaction_id,
    )
    return True


router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_CUSTOM_CONFIGS_FILE = _BASE_DIR / "custom_configs.json"

# 默认收费模式（可被 custom_configs.json 中 BILLING_PRICING 覆盖）
_DEFAULT_SKILL_UNLOCK = {"min_yuan": 98, "max_yuan": 198}
_DEFAULT_CREDIT_PACKAGES = [
    {"price_yuan": 198, "credits": 20000, "label": "198元 - 20000积分"},
    {"price_yuan": 498, "credits": 50000, "label": "498元 - 50000积分"},
    {"price_yuan": 998, "credits": 120000, "label": "998元 - 120000积分"},
]


def _get_billing_pricing() -> dict[str, Any]:
    """从 custom_configs.json 读取 BILLING_PRICING；缺失时返回默认。"""
    if not _CUSTOM_CONFIGS_FILE.exists():
        return {
            "skill_unlock": _DEFAULT_SKILL_UNLOCK,
            "credit_packages": _DEFAULT_CREDIT_PACKAGES,
        }
    try:
        data = json.loads(_CUSTOM_CONFIGS_FILE.read_text(encoding="utf-8"))
        cfg = (data.get("configs") or {}).get("BILLING_PRICING")
        if not isinstance(cfg, dict):
            return {
                "skill_unlock": _DEFAULT_SKILL_UNLOCK,
                "credit_packages": _DEFAULT_CREDIT_PACKAGES,
            }
        skill = cfg.get("skill_unlock")
        if isinstance(skill, dict):
            min_yuan = skill.get("min_yuan")
            max_yuan = skill.get("max_yuan")
            skill_unlock = {
                "min_yuan": int(min_yuan) if min_yuan is not None else _DEFAULT_SKILL_UNLOCK["min_yuan"],
                "max_yuan": int(max_yuan) if max_yuan is not None else _DEFAULT_SKILL_UNLOCK["max_yuan"],
            }
        else:
            skill_unlock = _DEFAULT_SKILL_UNLOCK

        packages = cfg.get("credit_packages")
        if isinstance(packages, list) and packages:
            out = []
            for p in packages:
                if not isinstance(p, dict):
                    continue
                credits = p.get("credits")
                if credits is None:
                    continue
                price_fen = p.get("price_fen")
                price = p.get("price_yuan") or p.get("price")
                if price_fen is not None:
                    label = (p.get("label") or "").strip() or f"{price_fen / 100:.2f}元 - {int(credits)}积分"
                    out.append({"price_fen": int(price_fen), "credits": int(credits), "label": label})
                elif price is not None:
                    label = (p.get("label") or "").strip() or f"{int(price)}元 - {int(credits)}积分"
                    out.append({"price_yuan": int(price), "credits": int(credits), "label": label})
            if out:
                credit_packages = out
            else:
                credit_packages = _DEFAULT_CREDIT_PACKAGES
        else:
            credit_packages = _DEFAULT_CREDIT_PACKAGES

        return {"skill_unlock": skill_unlock, "credit_packages": credit_packages}
    except Exception as e:
        logger.debug("BILLING_PRICING read failed: %s", e)
        return {
            "skill_unlock": _DEFAULT_SKILL_UNLOCK,
            "credit_packages": _DEFAULT_CREDIT_PACKAGES,
        }


@router.get("/api/billing/pricing", summary="软件收费模式（技能解锁价格 + 算力套餐）")
def get_billing_pricing(current_user: User = Depends(get_current_user)):
    """返回技能解锁价格区间与算力套餐列表，供前端展示。可在 custom_configs.json 的 configs.BILLING_PRICING 中覆盖。"""
    return _get_billing_pricing()


# ── 自有充值（独立于速推）────────────────────────────────────────────────────

def _use_independent_recharge() -> bool:
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    return edition == "online" and getattr(settings, "lobster_independent_auth", True)


@router.get("/api/recharge/qr-png", summary="将字符串转为 PNG 二维码（供微信 Native 支付链接展示，无需外链图床）")
def recharge_qr_png(data: str = Query(..., min_length=1, max_length=4096)):
    """无 JWT：仅用于展示 weixin:// 支付链接，便于 <img src> 跨域加载。"""
    try:
        import segno
    except ImportError as e:
        raise HTTPException(status_code=503, detail="未安装 segno，无法生成二维码") from e
    buf = BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=6, border=4)
    return Response(content=buf.getvalue(), media_type="image/png")


@router.get("/api/recharge/packages", summary="充值套餐列表（自有）")
def get_recharge_packages(current_user: User = Depends(get_current_user)):
    if not _use_independent_recharge():
        raise HTTPException(status_code=400, detail="当前未启用自有充值")
    pricing = _get_billing_pricing()
    return {"packages": pricing.get("credit_packages", _DEFAULT_CREDIT_PACKAGES)}


@router.get("/api/recharge/my-orders", summary="当前用户充值订单列表（消费记录用）")
def get_my_recharge_orders(
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    total = db.query(RechargeOrder).filter(RechargeOrder.user_id == current_user.id).count()
    rows = (
        db.query(RechargeOrder)
        .filter(RechargeOrder.user_id == current_user.id)
        .order_by(RechargeOrder.id.desc())
        .offset(max(0, offset))
        .limit(min(max(limit, 1), 200))
        .all()
    )
    items = [
        {
            "id": r.id,
            "out_trade_no": r.out_trade_no,
            "amount_yuan": r.amount_yuan,
            "amount_fen": r.amount_fen,
            "credits": r.credits,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "created_at_beijing": _dt_utc_naive_to_beijing_str(r.created_at),
            "paid_at": r.paid_at.isoformat() if r.paid_at else "",
            "paid_at_beijing": _dt_utc_naive_to_beijing_str(r.paid_at),
        }
        for r in rows
    ]
    return {"items": items, "total": total}


@router.get("/api/billing/credit-history", summary="积分变动记录（与 credit_ledger 一致：预扣/结算/退款/充值/LLM 等）")
def get_credit_history(
    limit: int = 100,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """与 users.credits 变动一一对应；展示 credit_ledger 全量（含 sutui_chat、pre_deduct、settle 等）。"""
    q = db.query(CreditLedger).filter(CreditLedger.user_id == current_user.id)
    total = q.count()
    off = max(0, offset)
    n = min(max(limit, 1), 200)
    rows = (
        q.order_by(CreditLedger.created_at.desc())
        .offset(off)
        .limit(n)
        .all()
    )
    out = []
    for r in rows:
        et = (r.entry_type or "").strip().lower()
        delta = ledger_display_delta(r)
        if delta > 0:
            type_label = "recharge" if et == "recharge" else "increase"
        else:
            type_label = "deduct"
        desc = (r.description or "").strip() or et or "积分变动"
        out.append({
            "time": r.created_at.isoformat() if r.created_at else "",
            "time_beijing": _dt_utc_naive_to_beijing_str(r.created_at),
            "type": type_label,
            "entry_type": r.entry_type or "",
            "amount": credits_json_float_signed(delta),
            "description": desc,
            "balance_after": credits_json_float(r.balance_after or 0),
            "out_trade_no": "",
        })
    return {"items": out, "total": total}


@router.get("/api/billing/credit-ledger", summary="积分流水（预扣/结算/退款/充值等，一行一条）")
def get_credit_ledger(
    limit: int = 100,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """完整审计流水：与 users.credits 变动一一对应（历史数据在表上线前为空）。"""
    q = db.query(CreditLedger).filter(CreditLedger.user_id == current_user.id)
    total = q.count()
    off = max(0, offset)
    n = min(max(limit, 1), 500)
    rows = (
        q.order_by(CreditLedger.created_at.desc())
        .offset(off)
        .limit(n)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "delta": credits_json_float_signed(ledger_display_delta(r)),
                "balance_after": credits_json_float(r.balance_after or 0),
                "entry_type": r.entry_type,
                "description": r.description or "",
                "ref_type": r.ref_type or "",
                "ref_id": r.ref_id or "",
                "meta": public_ledger_meta(r.meta if isinstance(r.meta, dict) else None),
                "time": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ],
        "total": total,
    }


class RechargeCreateBody(BaseModel):
    package_index: Optional[int] = None
    price_yuan: Optional[int] = None
    credits: Optional[int] = None


@router.post("/api/recharge/create", summary="创建充值订单（自有）")
def create_recharge_order(
    body: RechargeCreateBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _use_independent_recharge():
        raise HTTPException(status_code=400, detail="当前未启用自有充值")
    pricing = _get_billing_pricing()
    packages = pricing.get("credit_packages", _DEFAULT_CREDIT_PACKAGES)
    if body.package_index is not None:
        idx = int(body.package_index)
        if idx < 0 or idx >= len(packages):
            raise HTTPException(status_code=400, detail="无效套餐")
        p = packages[idx]
        if "price_fen" in p:
            amount_yuan = 0
            amount_fen = int(p["price_fen"])
            credits = p["credits"]
        else:
            amount_yuan = p["price_yuan"]
            amount_fen = 0
            credits = p["credits"]
    elif body.price_yuan is not None and body.credits is not None:
        amount_yuan = int(body.price_yuan)
        amount_fen = 0
        credits = int(body.credits)
        if amount_yuan <= 0 or credits <= 0:
            raise HTTPException(status_code=400, detail="金额与积分须为正数")
    else:
        raise HTTPException(status_code=400, detail="请选择套餐或指定 price_yuan + credits")
    out_trade_no = f"R{current_user.id}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    order = RechargeOrder(
        user_id=current_user.id,
        amount_yuan=amount_yuan,
        amount_fen=amount_fen,
        credits=credits,
        status="pending",
        out_trade_no=out_trade_no,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    payment_hint = getattr(settings, "lobster_recharge_payment_hint", None) or "请通过微信/支付宝转账并联系管理员完成到账，备注订单号。"
    _amount_display = (order.amount_fen / 100) if (order.amount_fen or 0) else order.amount_yuan
    return {
        "order_id": order.id,
        "out_trade_no": order.out_trade_no,
        "amount_yuan": _amount_display,
        "credits": order.credits,
        "status": order.status,
        "payment_info": payment_hint,
        "created_at": order.created_at.isoformat() if order.created_at else "",
    }


class RechargeCompleteBody(BaseModel):
    out_trade_no: Optional[str] = None
    order_id: Optional[int] = None


# ── 自建微信支付（不用速推）────────────────────────────────────────────────

@router.post("/api/recharge/wechat-create", summary="创建充值订单并调微信 Native 下单，返回扫码链接")
def create_wechat_recharge_order(
    body: RechargeCreateBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _use_independent_recharge():
        raise HTTPException(status_code=400, detail="当前未启用自有充值")
    if not _wechat_pay_configured():
        raise HTTPException(status_code=400, detail="未配置自建微信支付（wechat_mch_id/wechat_pay_apiv3_key/wechat_pay_serial_no/wechat_pay_private_key_path/wechat_app_id）")
    pricing = _get_billing_pricing()
    packages = pricing.get("credit_packages", _DEFAULT_CREDIT_PACKAGES)
    if body.package_index is not None:
        idx = int(body.package_index)
        if idx < 0 or idx >= len(packages):
            raise HTTPException(status_code=400, detail="无效套餐")
        p = packages[idx]
        if "price_fen" in p:
            amount_yuan = 0
            amount_fen = int(p["price_fen"])
            credits = p["credits"]
        else:
            amount_yuan = p["price_yuan"]
            amount_fen = 0
            credits = p["credits"]
    elif body.price_yuan is not None and body.credits is not None:
        amount_yuan = int(body.price_yuan)
        amount_fen = 0
        credits = int(body.credits)
        if amount_yuan <= 0 or credits <= 0:
            raise HTTPException(status_code=400, detail="金额与积分须为正数")
    else:
        raise HTTPException(status_code=400, detail="请选择套餐或指定 price_yuan + credits")
    out_trade_no = f"R{current_user.id}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    order = RechargeOrder(
        user_id=current_user.id,
        amount_yuan=amount_yuan,
        amount_fen=amount_fen,
        credits=credits,
        status="pending",
        out_trade_no=out_trade_no,
        payment_method="wechat",
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    base_url = _get_public_base_url()
    notify_url = f"{base_url}/api/recharge/wechat-notify"
    key_path = Path((getattr(settings, "wechat_pay_private_key_path", None) or "").strip())
    if not key_path.is_absolute():
        key_path = _BASE_DIR / key_path
    try:
        private_key = key_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("wechat pay private key read failed: %s", e)
        raise HTTPException(status_code=500, detail="微信支付商户私钥读取失败")
    apiv3_key = (getattr(settings, "wechat_pay_apiv3_key", None) or "").strip()[:32]
    if len(apiv3_key) != 32:
        logger.warning("wechat pay: WECHAT_PAY_APIV3_KEY length=%s (need 32). Check 商户平台-API安全.", len(apiv3_key))
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
            raise HTTPException(status_code=500, detail="微信支付公钥文件读取失败，请检查 wechat_pay_public_key_path")
    try:
        from wechatpayv3 import WeChatPay, WeChatPayType
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
        wxpay = WeChatPay(**kwargs)
        total_fen = (order.amount_fen or 0) or (order.amount_yuan * 100)
        # wechatpayv3 pay() 常见返回 (code, message)：code 为状态码，code_url 在 message 里；个别版本或返回 (code_url, _)
        first, second = wxpay.pay(
            description=f"龙虾积分充值-{credits}积分",
            out_trade_no=out_trade_no,
            amount={"total": total_fen},
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
            logger.warning("wechat pay response first=%s second=%s", first, second)
            raise HTTPException(status_code=502, detail="微信下单返回异常或缺少 code_url，请稍后重试")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("wechat pay create failed: %s", e)
        err_msg = str(e).strip()
        if "platform certificate" in err_msg.lower() or "404" in err_msg or "401" in err_msg or "certificates" in err_msg.lower():
            detail = (
                "微信支付平台证书获取失败（GET /v3/certificates 通常返回 404 表示鉴权失败）。"
                "请核对 .env：商户号(wechat_mch_id)、证书序列号(wechat_pay_serial_no)、APIv3 密钥(wechat_pay_apiv3_key)、"
                "商户私钥(wechat_pay_private_key_path)、关联 AppID(wechat_app_id) 是否与微信支付商户平台一致；"
                "或配置 wechat_pay_cert_dir 指向一目录，并从商户平台下载平台证书重命名为 cert.pem 放入该目录后重试。"
            )
        else:
            detail = "微信下单失败，请稍后重试"
        raise HTTPException(status_code=502, detail=detail)
    _amount_display = (order.amount_fen / 100) if (order.amount_fen or 0) else order.amount_yuan
    return {
        "order_id": order.id,
        "out_trade_no": order.out_trade_no,
        "amount_yuan": _amount_display,
        "credits": order.credits,
        "code_url": code_url,
        "status": order.status,
    }


@router.post(
    "/api/recharge/wechat-notify",
    summary="微信支付异步回调（验签解密后完成订单加积分）",
)
async def wechat_pay_notify(request: Request, db: Session = Depends(get_db)):
    """
    安全策略：① 依赖 wechatpayv3 验签，伪造请求无法通过；② 回调金额必须与订单金额一致否则返回 fail 不加积分；
    ③ 已支付订单再次回调直接返回 success 不重复加积分（防重放）；④ 每笔到账记录 callback_amount_fen、wechat_transaction_id 与日志。
    """
    if not _wechat_pay_configured():
        logger.warning("wechat_notify wechat pay not configured")
        return PlainTextResponse("fail", status_code=500)
    raw = await request.body()
    headers = dict(request.headers)
    logger.info(
        "wechat_notify received method=%s content_length=%s has_signature=%s",
        request.method,
        len(raw),
        "Wechatpay-Signature" in headers or "wechatpay-signature" in [h.lower() for h in headers],
    )
    key_path = Path((getattr(settings, "wechat_pay_private_key_path", None) or "").strip())
    if not key_path.is_absolute():
        key_path = _BASE_DIR / key_path
    try:
        private_key = key_path.read_text(encoding="utf-8")
    except Exception:
        return PlainTextResponse("fail", status_code=500)
    apiv3_key_raw = (getattr(settings, "wechat_pay_apiv3_key", None) or "").strip()
    if len(apiv3_key_raw) != 32:
        logger.warning("wechat_notify WECHAT_PAY_APIV3_KEY len=%s (must be 32), callback decrypt may fail", len(apiv3_key_raw))
    apiv3_key = apiv3_key_raw[:32]
    public_key_path_raw = (getattr(settings, "wechat_pay_public_key_path", None) or "").strip()
    public_key_id = (getattr(settings, "wechat_pay_public_key_id", None) or "").strip()
    public_key_content = None
    if public_key_path_raw and public_key_id:
        pub_path = Path(public_key_path_raw)
        if not pub_path.is_absolute():
            pub_path = _BASE_DIR / pub_path
        try:
            public_key_content = pub_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    if not public_key_content or not public_key_id:
        logger.warning("wechat_notify WECHAT_PAY_PUBLIC_KEY_PATH/ID not set, callback verify may need platform cert")
    try:
        from wechatpayv3 import WeChatPay, WeChatPayType
        kwargs = dict(
            wechatpay_type=WeChatPayType.NATIVE,
            mchid=(getattr(settings, "wechat_mch_id", None) or "").strip(),
            private_key=private_key,
            cert_serial_no=(getattr(settings, "wechat_pay_serial_no", None) or "").strip(),
            apiv3_key=apiv3_key,
            appid=(getattr(settings, "wechat_app_id", None) or "").strip(),
        )
        if public_key_content and public_key_id:
            kwargs["public_key"] = public_key_content
            kwargs["public_key_id"] = public_key_id
        wxpay = WeChatPay(**kwargs)
        result = wxpay.callback(headers, raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception as e:
        import traceback
        wechat_headers = {k: v[:80] + "..." if len(str(v)) > 80 else v for k, v in headers.items() if "wechat" in k.lower() or "signature" in k.lower()}
        logger.warning(
            "wechat_notify callback verify failed: %s (type=%s) wechat_headers_keys=%s",
            e, type(e).__name__, list(wechat_headers.keys()),
        )
        logger.debug("wechat_notify verify traceback: %s", traceback.format_exc())
        return PlainTextResponse("fail", status_code=400)
    event_type = result.get("event_type") if isinstance(result, dict) else None
    logger.info("wechat_notify decoded event_type=%s result_keys=%s", event_type, list(result.keys()) if isinstance(result, dict) else None)
    if not result or event_type != "TRANSACTION.SUCCESS":
        logger.info("wechat_notify skip non TRANSACTION.SUCCESS event_type=%s", event_type)
        return PlainTextResponse("success")
    # 回调解密后：金额在 amount.total(分)，单号在 out_trade_no、transaction_id
    res = result.get("resource") if isinstance(result.get("resource"), dict) else result
    payload = res if isinstance(res, dict) else result
    out_trade_no = (payload.get("out_trade_no") or result.get("out_trade_no") or "").strip()
    if not out_trade_no:
        return PlainTextResponse("success")
    logger.info("wechat_notify lookup order out_trade_no=%s", out_trade_no)
    order = db.query(RechargeOrder).filter(RechargeOrder.out_trade_no == out_trade_no).first()
    if not order:
        logger.warning("wechat_notify order not found out_trade_no=%s", out_trade_no)
        return PlainTextResponse("success")
    if order.status == "paid":
        logger.info("wechat_notify order already paid out_trade_no=%s order_id=%s", out_trade_no, order.id)
        return PlainTextResponse("success")
    # 安全校验：回调金额必须与订单金额一致，否则拒绝到账
    amount_info = payload.get("amount") if isinstance(payload.get("amount"), dict) else None
    callback_total_fen = amount_info.get("total") if amount_info is not None else None
    if callback_total_fen is not None:
        callback_total_fen = int(callback_total_fen)
    expected_fen = (order.amount_fen or 0) or (order.amount_yuan * 100)
    if callback_total_fen is None:
        logger.error("wechat_notify amount missing out_trade_no=%s result_keys=%s", out_trade_no, list(result.keys()) if isinstance(result, dict) else None)
        return PlainTextResponse("fail", status_code=400)
    if callback_total_fen != expected_fen:
        logger.error(
            "wechat_notify amount_mismatch out_trade_no=%s order_id=%s callback_fen=%s expected_fen=%s reject",
            out_trade_no, order.id, callback_total_fen, expected_fen,
        )
        return PlainTextResponse("fail", status_code=400)
    wechat_transaction_id = (payload.get("transaction_id") or result.get("transaction_id") or "").strip() or None
    if not _apply_wechat_paid_to_order(order, callback_total_fen, wechat_transaction_id, db):
        return PlainTextResponse("fail", status_code=400)
    logger.info("wechat_notify success order_id=%s out_trade_no=%s credits=%s", order.id, out_trade_no, order.credits)
    return PlainTextResponse("success")


@router.get("/api/recharge/wechat-query", summary="无公网时轮询：服务器主动查微信订单，已支付则入账（不依赖回调）")
def wechat_query_order(
    out_trade_no: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    无公网地址、微信无法回调时：前端在用户扫码支付后轮询本接口；
    服务器调微信「查询订单」API，若已支付则校验金额并加积分，返回 status=paid。
    """
    if not _wechat_pay_configured():
        raise HTTPException(status_code=400, detail="未配置微信支付")
    out_trade_no = (out_trade_no or "").strip()
    if not out_trade_no:
        raise HTTPException(status_code=400, detail="请传 out_trade_no")
    order = db.query(RechargeOrder).filter(
        RechargeOrder.out_trade_no == out_trade_no,
        RechargeOrder.user_id == current_user.id,
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在或无权查询")
    if order.status == "paid":
        return {"status": "paid", "credits": order.credits, "order_id": order.id}
    key_path = Path((getattr(settings, "wechat_pay_private_key_path", None) or "").strip())
    if not key_path.is_absolute():
        key_path = _BASE_DIR / key_path
    try:
        private_key = key_path.read_text(encoding="utf-8")
    except Exception:
        raise HTTPException(status_code=500, detail="商户私钥读取失败")
    apiv3_key = (getattr(settings, "wechat_pay_apiv3_key", None) or "").strip()[:32]
    public_key_path_raw = (getattr(settings, "wechat_pay_public_key_path", None) or "").strip()
    public_key_id = (getattr(settings, "wechat_pay_public_key_id", None) or "").strip()
    public_key_content: Optional[str] = None
    if public_key_path_raw and public_key_id:
        pub_path = Path(public_key_path_raw)
        if not pub_path.is_absolute():
            pub_path = _BASE_DIR / pub_path
        try:
            public_key_content = pub_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    try:
        from wechatpayv3 import WeChatPay, WeChatPayType
        mchid = (getattr(settings, "wechat_mch_id", None) or "").strip()
        kwargs = dict(
            wechatpay_type=WeChatPayType.NATIVE,
            mchid=mchid,
            private_key=private_key,
            cert_serial_no=(getattr(settings, "wechat_pay_serial_no", None) or "").strip(),
            apiv3_key=apiv3_key,
            appid=(getattr(settings, "wechat_app_id", None) or "").strip(),
        )
        if public_key_content and public_key_id:
            kwargs["public_key"] = public_key_content
            kwargs["public_key_id"] = public_key_id
        wxpay = WeChatPay(**kwargs)
        try:
            result = wxpay.query_order(out_trade_no=out_trade_no, mchid=mchid)
        except AttributeError:
            result = wxpay.query(out_trade_no=out_trade_no, mchid=mchid)
    except Exception as e:
        logger.warning("wechat query order failed: %s", e)
        return {"status": "pending", "message": "查单失败，请稍后再试"}
    # result 可能是 (code, body) 或直接 body
    if isinstance(result, (list, tuple)) and len(result) >= 2:
        code, body = result[0], result[1]
        if code != 200:
            return {"status": "pending"}
        resp = body if isinstance(body, dict) else (json.loads(body) if isinstance(body, str) else {})
    else:
        resp = result if isinstance(result, dict) else {}
    trade_state = (resp.get("trade_state") or "").strip()
    if trade_state != "SUCCESS":
        return {"status": "pending"}
    amount_info = resp.get("amount")
    paid_fen = int(amount_info.get("total")) if isinstance(amount_info, dict) and amount_info.get("total") is not None else None
    if paid_fen is None:
        return {"status": "pending", "message": "查单结果无金额"}
    wechat_transaction_id = (resp.get("transaction_id") or "").strip() or None
    if not _apply_wechat_paid_to_order(order, paid_fen, wechat_transaction_id, db):
        return {"status": "pending", "message": "金额校验未通过"}
    db.refresh(order)
    return {"status": "paid", "credits": order.credits, "order_id": order.id}


@router.post("/api/recharge/complete", summary="完成充值（管理员/回调：到账加积分）")
def complete_recharge(
    body: RechargeCompleteBody,
    x_admin_secret: Optional[str] = Header(None, alias="X-Admin-Secret"),
    db: Session = Depends(get_db),
):
    secret = (getattr(settings, "lobster_recharge_admin_secret", None) or "").strip()
    if not secret or (x_admin_secret or "").strip() != secret:
        raise HTTPException(status_code=403, detail="需要管理员密钥")
    if body.out_trade_no:
        order = db.query(RechargeOrder).filter(RechargeOrder.out_trade_no == body.out_trade_no.strip()).first()
    elif body.order_id is not None:
        order = db.query(RechargeOrder).filter(RechargeOrder.id == body.order_id).first()
    else:
        raise HTTPException(status_code=400, detail="请提供 out_trade_no 或 order_id")
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status == "paid":
        return {"ok": True, "message": "订单已支付过", "order_id": order.id}
    user = db.query(User).filter(User.id == order.user_id).first()
    if not user:
        raise HTTPException(status_code=500, detail="用户不存在")
    add_credits = quantize_credits(order.credits or 0)
    user.credits = quantize_credits(user.credits or 0) + add_credits
    bal = quantize_credits(user.credits)
    append_credit_ledger(
        db,
        user.id,
        add_credits,
        "recharge",
        bal,
        description="充值到账（管理员 complete）",
        ref_type="recharge_order",
        ref_id=(order.out_trade_no or "")[:128],
        meta={"order_id": order.id, "source": "admin_complete"},
    )
    order.status = "paid"
    from datetime import datetime
    order.paid_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "message": f"已到账 {order.credits} 积分", "order_id": order.id}
