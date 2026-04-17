"""软件收费模式配置与展示：技能解锁价格、算力套餐（积分兑换比例）；自有充值订单；付呗聚合支付。"""
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
from ..services.fubei_pay import (
    fubei_configured,
    fubei_precreate,
    fubei_query_order,
    verify_callback_sign,
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


def _get_public_base_url() -> str:
    """支付回调等用。未配置 PUBLIC_BASE_URL 时用本机 IP:PORT。"""
    return get_effective_public_base_url()


def _apply_paid_to_order(
    order: RechargeOrder,
    paid_total_fen: int,
    channel_transaction_id: Optional[str],
    db: Session,
    channel_label: str = "付呗聚合支付",
) -> bool:
    """校验金额一致后写入审计、加积分、置已支付。返回 True 表示已处理，False 表示金额不符未处理。"""
    from datetime import datetime
    expected_fen = (order.amount_fen or 0) or (order.amount_yuan * 100)
    if paid_total_fen != expected_fen:
        logger.error(
            "paid amount_mismatch out_trade_no=%s order_id=%s paid_fen=%s expected_fen=%s",
            order.out_trade_no, order.id, paid_total_fen, expected_fen,
        )
        return False
    order.callback_amount_fen = paid_total_fen
    order.wechat_transaction_id = channel_transaction_id
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
            description=f"充值到账（{channel_label}）",
            ref_type="recharge_order",
            ref_id=(order.out_trade_no or "")[:128],
            meta={
                "order_id": order.id,
                "amount_fen": paid_total_fen,
                "channel_transaction_id": channel_transaction_id,
            },
        )
    order.status = "paid"
    order.paid_at = datetime.utcnow()
    db.commit()
    logger.info(
        "paid (query or notify) order_id=%s out_trade_no=%s paid_fen=%s credits_granted=%s user_id=%s transaction_id=%s",
        order.id, order.out_trade_no, paid_total_fen, order.credits, order.user_id, channel_transaction_id,
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


# ── 付呗聚合支付（替代自建微信支付）─────────────────────────────────────────

@router.post("/api/recharge/fubei-create", summary="创建充值订单并调付呗预下单，返回聚合收款码")
async def create_fubei_recharge_order(
    body: RechargeCreateBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _use_independent_recharge():
        raise HTTPException(status_code=400, detail="当前未启用自有充值")
    if not fubei_configured():
        raise HTTPException(status_code=400, detail="未配置付呗支付（FUBEI_APP_ID / FUBEI_APP_SECRET）")
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
        payment_method="fubei",
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    total_yuan = (order.amount_fen / 100) if (order.amount_fen or 0) else float(order.amount_yuan)
    base_url = _get_public_base_url()
    notify_url = f"{base_url}/api/recharge/fubei-notify"
    front_url = base_url.rstrip("/") + "/"
    try:
        result = await fubei_precreate(
            merchant_order_sn=out_trade_no,
            total_amount=total_yuan,
            body=f"龙虾积分充值-{credits}积分",
            notify_url=notify_url,
            success_url=front_url,
            fail_url=front_url,
            cancel_url=front_url,
            attach=str(order.id),
        )
    except Exception as e:
        logger.exception("[fubei] precreate failed: %s", e)
        raise HTTPException(status_code=502, detail="付呗下单失败，请稍后重试")
    code = result.get("result_code")
    if code != 200:
        detail = result.get("result_message") or "付呗下单返回异常"
        logger.warning("[fubei] precreate error: code=%s msg=%s", code, detail)
        raise HTTPException(status_code=502, detail=detail)
    data = result.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    qr_code = (
        data.get("qr_code") or data.get("code_url") or data.get("qr_url")
        or data.get("prepay_url") or ""
    ).strip()
    fubei_order_sn = (data.get("order_sn") or data.get("sn") or "").strip()
    if not qr_code:
        logger.warning(
            "[fubei] precreate no qr_code in data=%s values=%s",
            list(data.keys()),
            {k: (v[:60] if isinstance(v, str) else v) for k, v in data.items()} if isinstance(data, dict) else data,
        )
        raise HTTPException(
            status_code=502,
            detail="付呗下单成功但未返回收款码（prepay_url 为空），请检查付呗商户后台是否已开通聚合收款码功能",
        )
    return {
        "order_id": order.id,
        "out_trade_no": order.out_trade_no,
        "amount_yuan": total_yuan,
        "credits": order.credits,
        "qr_code": qr_code,
        "fubei_order_sn": fubei_order_sn,
        "status": order.status,
    }


@router.post(
    "/api/recharge/fubei-notify",
    summary="付呗支付异步回调（验签后完成订单加积分）",
)
async def fubei_pay_notify(request: Request, db: Session = Depends(get_db)):
    """
    安全策略：① MD5 签名校验；② 回调金额与订单金额一致才加积分；
    ③ 已支付订单再次回调返回 success 不重复加积分（防重放）。
    """
    if not fubei_configured():
        logger.warning("[fubei] notify: fubei not configured")
        return PlainTextResponse("fail", status_code=500)
    raw = await request.body()
    logger.info("[fubei] notify received content_length=%s", len(raw))
    try:
        payload = json.loads(raw)
    except Exception:
        logger.warning("[fubei] notify: invalid JSON")
        return PlainTextResponse("fail", status_code=400)
    if not verify_callback_sign(payload):
        logger.warning("[fubei] notify: sign verification failed")
        return PlainTextResponse("fail", status_code=400)
    biz_raw = payload.get("biz_content") or payload.get("data") or "{}"
    if isinstance(biz_raw, str):
        try:
            biz = json.loads(biz_raw)
        except Exception:
            biz = {}
    else:
        biz = biz_raw if isinstance(biz_raw, dict) else {}
    # 付呗回调中的业务数据：order_status, merchant_order_sn, order_sn, total_fee, net_amount 等
    order_status = (biz.get("order_status") or payload.get("order_status") or "").strip().upper()
    if order_status not in ("SUCCESS", "TRADE_SUCCESS"):
        logger.info("[fubei] notify skip non-success status=%s", order_status)
        return PlainTextResponse("success")
    out_trade_no = (biz.get("merchant_order_sn") or payload.get("merchant_order_sn") or "").strip()
    if not out_trade_no:
        logger.warning("[fubei] notify: missing merchant_order_sn")
        return PlainTextResponse("success")
    order = db.query(RechargeOrder).filter(RechargeOrder.out_trade_no == out_trade_no).first()
    if not order:
        logger.warning("[fubei] notify: order not found out_trade_no=%s", out_trade_no)
        return PlainTextResponse("success")
    if order.status == "paid":
        return PlainTextResponse("success")
    # 金额校验：total_fee 单位为元
    total_fee = biz.get("total_fee") or biz.get("total_amount") or payload.get("total_fee")
    if total_fee is not None:
        callback_fen = int(round(float(total_fee) * 100))
    else:
        logger.error("[fubei] notify: amount missing out_trade_no=%s", out_trade_no)
        return PlainTextResponse("fail", status_code=400)
    fubei_order_sn = (biz.get("order_sn") or payload.get("order_sn") or "").strip() or None
    if not _apply_paid_to_order(order, callback_fen, fubei_order_sn, db, channel_label="付呗聚合支付"):
        return PlainTextResponse("fail", status_code=400)
    logger.info("[fubei] notify success order_id=%s out_trade_no=%s credits=%s", order.id, out_trade_no, order.credits)
    return PlainTextResponse("success")


@router.get("/api/recharge/fubei-query", summary="主动查询付呗订单状态，已支付则入账")
async def fubei_query_recharge_order(
    out_trade_no: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not fubei_configured():
        raise HTTPException(status_code=400, detail="未配置付呗支付")
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
    try:
        result = await fubei_query_order(merchant_order_sn=out_trade_no)
    except Exception as e:
        logger.warning("[fubei] query order failed: %s", e)
        return {"status": "pending", "message": "查单失败，请稍后再试"}
    code = result.get("result_code")
    if code != 200:
        return {"status": "pending", "message": result.get("result_message", "")}
    data = result.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    order_status = (data.get("order_status") or "").strip().upper()
    if order_status not in ("SUCCESS", "TRADE_SUCCESS"):
        return {"status": "pending"}
    total_fee = data.get("total_fee") or data.get("total_amount")
    if total_fee is not None:
        paid_fen = int(round(float(total_fee) * 100))
    else:
        return {"status": "pending", "message": "查单结果无金额"}
    fubei_order_sn = (data.get("order_sn") or "").strip() or None
    if not _apply_paid_to_order(order, paid_fen, fubei_order_sn, db, channel_label="付呗聚合支付"):
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
