"""服务器上对指定 out_trade_no 执行付呗查单并入账（不校验 user，管理员用）。

用法: python scripts/fubei_query_order.py <out_trade_no>
"""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.core.config import settings
from backend.app.db import SessionLocal
from backend.app.models import RechargeOrder
from backend.app.services.fubei_pay import fubei_configured, fubei_query_order
from backend.app.api.billing import _apply_paid_to_order


async def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/fubei_query_order.py <out_trade_no>")
        sys.exit(2)
    out_trade_no = sys.argv[1].strip()
    if not out_trade_no:
        print("out_trade_no 为空")
        sys.exit(2)

    if not fubei_configured():
        print("未配置付呗支付")
        sys.exit(1)

    db = SessionLocal()
    try:
        order = db.query(RechargeOrder).filter(RechargeOrder.out_trade_no == out_trade_no).first()
        if not order:
            print("订单不存在")
            sys.exit(1)
        if order.status == "paid":
            print("已支付", "order_id=%s credits=%s" % (order.id, order.credits))
            return

        result = await fubei_query_order(merchant_order_sn=out_trade_no)
        code = result.get("result_code")
        if code != 200:
            print("付呗查单失败", result.get("result_message", ""))
            sys.exit(1)

        data = result.get("data") or {}
        if isinstance(data, str):
            data = json.loads(data)

        order_status = (data.get("order_status") or "").strip().upper()
        if order_status not in ("SUCCESS", "TRADE_SUCCESS"):
            print("未支付", "order_status=%s" % order_status)
            sys.exit(1)

        total_fee = data.get("total_fee") or data.get("total_amount")
        if total_fee is None:
            print("查单结果无金额")
            sys.exit(1)
        paid_fen = int(round(float(total_fee) * 100))
        fubei_order_sn = (data.get("order_sn") or "").strip() or None

        if not _apply_paid_to_order(order, paid_fen, fubei_order_sn, db, channel_label="付呗聚合支付"):
            print("金额校验未通过")
            sys.exit(1)

        db.refresh(order)
        print("已入账", "order_id=%s credits=%s order_sn=%s" % (order.id, order.credits, fubei_order_sn))
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
