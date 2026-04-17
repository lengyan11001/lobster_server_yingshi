"""
付呗支付回调安全测试：签名校验、金额校验、防重放、审计字段。
"""
import hashlib
import json
import os
import sys
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Base, get_db
from app.models import RechargeOrder, User
from app.create_app import create_app


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


FAKE_APP_SECRET = "test_secret_1234567890"


def _make_sign(params: dict) -> str:
    parts = []
    for k in sorted(params.keys()):
        if k == "sign":
            continue
        parts.append(f"{k}={params[k]}")
    raw = "&".join(parts) + FAKE_APP_SECRET
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


@pytest.fixture(scope="module")
def app():
    Base.metadata.create_all(bind=engine)
    _app = create_app()
    _app.dependency_overrides[get_db] = override_get_db
    with patch("app.services.fubei_pay.fubei_configured", return_value=True), \
         patch("app.services.fubei_pay._cfg_app_secret", return_value=FAKE_APP_SECRET):
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
        payment_method="fubei",
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _create_user(db, user_id=1, credits=100):
    u = User(id=user_id, email=f"u{user_id}@test.com", hashed_password="x", credits=credits, role="user")
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


def _build_callback_body(merchant_order_sn: str, total_fee: float, order_sn: str = "FB123"):
    biz = json.dumps({
        "order_status": "SUCCESS",
        "merchant_order_sn": merchant_order_sn,
        "order_sn": order_sn,
        "total_fee": total_fee,
    })
    params = {"biz_content": biz, "method": "callback", "nonce": "test123"}
    params["sign"] = _make_sign(params)
    return params


class TestFubeiNotifySecurity:

    def test_bad_sign_rejected(self, client, db_session):
        _create_user(db_session, user_id=1, credits=100)
        _create_pending_order(db_session, out_trade_no="R1_bad_sign", amount_fen=1, credits=1)
        body = _build_callback_body("R1_bad_sign", 0.01)
        body["sign"] = "BADSIGNATURE"
        r = client.post("/api/recharge/fubei-notify", json=body)
        assert r.status_code == 400

    def test_amount_mismatch_rejected(self, client, db_session):
        _create_user(db_session, user_id=1, credits=100)
        _create_pending_order(db_session, out_trade_no="R1_mismatch", amount_fen=1, credits=1)
        body = _build_callback_body("R1_mismatch", 998.00)
        r = client.post("/api/recharge/fubei-notify", json=body)
        assert r.status_code == 400
        order = db_session.query(RechargeOrder).filter(RechargeOrder.out_trade_no == "R1_mismatch").first()
        assert order.status == "pending"

    def test_amount_match_success(self, client, db_session):
        _create_user(db_session, user_id=1, credits=100)
        _create_pending_order(db_session, out_trade_no="R1_ok", amount_fen=1, credits=1)
        body = _build_callback_body("R1_ok", 0.01)
        r = client.post("/api/recharge/fubei-notify", json=body)
        assert r.status_code == 200
        order = db_session.query(RechargeOrder).filter(RechargeOrder.out_trade_no == "R1_ok").first()
        assert order.status == "paid"
        assert order.callback_amount_fen == 1

    def test_replay_no_double_credits(self, client, db_session):
        _create_user(db_session, user_id=1, credits=100)
        order = _create_pending_order(db_session, out_trade_no="R1_replay", amount_fen=1, credits=1)
        order.status = "paid"
        order.callback_amount_fen = 1
        db_session.commit()
        body = _build_callback_body("R1_replay", 0.01)
        r = client.post("/api/recharge/fubei-notify", json=body)
        assert r.status_code == 200
        user = db_session.query(User).filter(User.id == 1).first()
        assert user.credits == 100
