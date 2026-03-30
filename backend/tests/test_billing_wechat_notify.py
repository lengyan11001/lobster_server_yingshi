"""
微信支付回调安全测试：金额校验、防重放、审计字段与日志。
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 确保 backend 包可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Base, get_db
from app.models import RechargeOrder, User
from app.create_app import create_app


# 临时文件 SQLite，使 TestClient 与 fixture 共用同一库（:memory: 每连接独立）
import tempfile
_test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_test_db.close()
TEST_DATABASE_URL = f"sqlite:///{_test_db.name}"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module")
def app():
    Base.metadata.create_all(bind=engine)
    _app = create_app()
    _app.dependency_overrides[get_db] = override_get_db
    # 测试环境无真实证书，令回调逻辑认为已配置并仅依赖 mock 的 WeChatPay.callback
    with patch("app.api.billing._wechat_pay_configured", return_value=True), \
         patch("pathlib.Path.read_text", return_value="fake-pem"):
        yield _app
    _app.dependency_overrides.clear()
    try:
        os.unlink(_test_db.name)
    except Exception:
        pass


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _create_pending_order(db, user_id=1, out_trade_no="R1_999_abc", amount_fen=1, amount_yuan=0, credits=1):
    order = RechargeOrder(
        user_id=user_id,
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
    return order


def _create_user(db, user_id=1, credits=100):
    u = User(
        id=user_id,
        email=f"u{user_id}@test.com",
        hashed_password="x",
        credits=credits,
        role="user",
    )
    try:
        db.add(u)
        db.commit()
        db.refresh(u)
    except Exception:
        db.rollback()
        existing = db.query(User).filter(User.id == user_id).first()
        if existing:
            existing.credits = credits
            db.commit()
        return db.query(User).filter(User.id == user_id).first()
    return u


def _mock_wechat_pay_callback(result_payload):
    """让 WeChatPay().callback() 返回指定 payload，模拟验签解密通过。"""
    mock_instance = MagicMock()
    mock_instance.callback.return_value = result_payload
    # billing 内 from wechatpayv3 import WeChatPay, WeChatPayType；patch 模块使 import 得到 mock
    mock_module = MagicMock()
    mock_module.WeChatPay = MagicMock(return_value=mock_instance)
    mock_module.WeChatPayType = MagicMock(NATIVE="NATIVE")
    return patch.dict("sys.modules", {"wechatpayv3": mock_module})


class TestWechatNotifySecurity:
    """回调安全：金额校验、防重放、审计。"""

    def test_amount_mismatch_rejected_and_no_credits(self, client, db_session):
        """回调金额与订单不一致：返回 fail，不加积分，订单仍 pending。"""
        _create_user(db_session, user_id=1, credits=100)
        _create_pending_order(db_session, out_trade_no="R1_1000_xyz", amount_fen=1, credits=1)
        # 伪造回调：说付了 99800 分，想骗高额积分
        payload = {
            "event_type": "TRANSACTION.SUCCESS",
            "out_trade_no": "R1_1000_xyz",
            "amount": {"total": 99800},
            "transaction_id": "wx_fake_998",
        }
        with _mock_wechat_pay_callback(payload):
            r = client.post("/api/recharge/wechat-notify", content="{}", headers={"Content-Type": "application/json"})
        assert r.status_code == 400
        assert r.text.strip().lower() == "fail"
        order = db_session.query(RechargeOrder).filter(RechargeOrder.out_trade_no == "R1_1000_xyz").first()
        assert order.status == "pending"
        assert order.callback_amount_fen is None
        user = db_session.query(User).filter(User.id == 1).first()
        assert user.credits == 100

    def test_amount_match_success_and_audit_fields_set(self, client, db_session):
        """回调金额与订单一致：成功，加积分，写审计字段。"""
        _create_user(db_session, user_id=1, credits=100)
        _create_pending_order(db_session, out_trade_no="R1_2000_ok", amount_fen=1, credits=1)
        payload = {
            "event_type": "TRANSACTION.SUCCESS",
            "out_trade_no": "R1_2000_ok",
            "amount": {"total": 1},
            "transaction_id": "wx_real_1",
        }
        with _mock_wechat_pay_callback(payload):
            r = client.post("/api/recharge/wechat-notify", content="{}", headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.text.strip().lower() == "success"
        order = db_session.query(RechargeOrder).filter(RechargeOrder.out_trade_no == "R1_2000_ok").first()
        assert order.status == "paid"
        assert order.callback_amount_fen == 1
        assert order.wechat_transaction_id == "wx_real_1"
        user = db_session.query(User).filter(User.id == 1).first()
        assert user.credits == 101

    def test_replay_already_paid_returns_success_no_double_credits(self, client, db_session):
        """已支付订单再次回调：返回 success，不重复加积分。"""
        _create_user(db_session, user_id=1, credits=100)
        order = _create_pending_order(db_session, out_trade_no="R1_3000_replay", amount_fen=1, credits=1)
        order.status = "paid"
        order.callback_amount_fen = 1
        order.wechat_transaction_id = "wx_first"
        db_session.commit()
        payload = {
            "event_type": "TRANSACTION.SUCCESS",
            "out_trade_no": "R1_3000_replay",
            "amount": {"total": 1},
            "transaction_id": "wx_first",
        }
        with _mock_wechat_pay_callback(payload):
            r = client.post("/api/recharge/wechat-notify", content="{}", headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.text.strip().lower() == "success"
        user = db_session.query(User).filter(User.id == 1).first()
        assert user.credits == 100

    def test_order_not_found_returns_success_no_leak(self, client, db_session):
        """订单不存在：返回 success，不泄露是否存在。"""
        payload = {
            "event_type": "TRANSACTION.SUCCESS",
            "out_trade_no": "R999_9999_nonexist",
            "amount": {"total": 1},
            "transaction_id": "wx_any",
        }
        with _mock_wechat_pay_callback(payload):
            r = client.post("/api/recharge/wechat-notify", content="{}", headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.text.strip().lower() == "success"

    def test_missing_amount_in_callback_returns_fail(self, client, db_session):
        """回调体缺少 amount：返回 fail。"""
        _create_user(db_session, user_id=1, credits=100)
        _create_pending_order(db_session, out_trade_no="R1_4000_noamt", amount_fen=1, credits=1)
        payload = {
            "event_type": "TRANSACTION.SUCCESS",
            "out_trade_no": "R1_4000_noamt",
            "transaction_id": "wx_no_amount",
        }
        with _mock_wechat_pay_callback(payload):
            r = client.post("/api/recharge/wechat-notify", content="{}", headers={"Content-Type": "application/json"})
        assert r.status_code == 400
        assert r.text.strip().lower() == "fail"
        order = db_session.query(RechargeOrder).filter(RechargeOrder.out_trade_no == "R1_4000_noamt").first()
        assert order.status == "pending"

    def test_yuan_order_amount_check(self, client, db_session):
        """按元计费订单：expected_fen = amount_yuan * 100，回调金额一致才通过。"""
        _create_user(db_session, user_id=1, credits=0)
        _create_pending_order(db_session, out_trade_no="R1_5000_yuan", amount_fen=0, amount_yuan=198, credits=20000)
        # 回调 19800 分 = 198 元，应成功
        payload = {
            "event_type": "TRANSACTION.SUCCESS",
            "out_trade_no": "R1_5000_yuan",
            "amount": {"total": 19800},
            "transaction_id": "wx_198",
        }
        with _mock_wechat_pay_callback(payload):
            r = client.post("/api/recharge/wechat-notify", content="{}", headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.text.strip().lower() == "success"
        order = db_session.query(RechargeOrder).filter(RechargeOrder.out_trade_no == "R1_5000_yuan").first()
        assert order.status == "paid"
        assert order.callback_amount_fen == 19800
        user = db_session.query(User).filter(User.id == 1).first()
        assert user.credits == 20000
