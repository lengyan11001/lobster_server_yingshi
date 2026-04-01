from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    credits: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal("99999.0000"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    preferred_model: Mapped[str] = mapped_column(String(128), default="openclaw", nullable=False)
    """速推登录后下发的 token，用于调用速推统一接口。"""
    sutui_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    """自建微信登录：开放平台 openid，用于扫码登录关联用户。"""
    wechat_openid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    """注册时客户端所在安装包的品牌标记（与 LOBSTER_BRAND_MARK / brands.json 的 marks 键一致，如 bihuo、yingshi）。"""
    brand_mark: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class CapabilityConfig(Base):
    __tablename__ = "capability_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    capability_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    upstream: Mapped[str] = mapped_column(String(64), nullable=False, default="sutui")
    upstream_tool: Mapped[str] = mapped_column(String(128), nullable=False)
    arg_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    extra_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    unit_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class CapabilityCallLog(Base):
    __tablename__ = "capability_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    upstream: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    upstream_tool: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    credits_charged: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal("0.0000"), nullable=False)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    request_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    response_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    chat_session_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    chat_context_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)


class ToolCallLog(Base):
    """Every MCP tool invocation from chat sessions."""
    __tablename__ = "tool_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    arguments: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_urls: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ChatTurnLog(Base):
    __tablename__ = "chat_turn_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    context_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


# ── Asset / Publish models ────────────────────────────────────────

class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    asset_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PublishAccount(Base):
    __tablename__ = "publish_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    nickname: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    browser_profile: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class WecomConfig(Base):
    """企业微信应用配置：支持多应用，每应用一个回调 path，用于验签与加解密。"""
    __tablename__ = "wecom_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="默认应用")
    callback_path: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(255), nullable=False)
    encoding_aes_key: Mapped[str] = mapped_column(String(255), nullable=False)
    corp_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    """应用 secret，用于获取 access_token 并调用「发送应用消息」接口（轮询模式下推送回复）。"""
    secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    """应用 AgentId（数字），发送应用消息时必填；未填则依赖消息体内的 AgentID。"""
    agent_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    product_knowledge: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class MessengerConfig(Base):
    """Facebook Messenger：多应用配置，每应用独立 callback_path、Verify Token、App Secret、Page Token。"""

    __tablename__ = "messenger_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="Messenger")
    callback_path: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    verify_token: Mapped[str] = mapped_column(String(255), nullable=False)
    app_secret: Mapped[str] = mapped_column(String(255), nullable=False)
    page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    page_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    product_knowledge: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class WecomPendingMessage(Base):
    """待处理消息队列：回调解密后入队，本地轮询拉取并提交回复后由云端调用企微发送接口推送。"""
    __tablename__ = "wecom_pending_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    wecom_config_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    from_user: Mapped[str] = mapped_column(String(128), nullable=False)
    to_user: Mapped[str] = mapped_column(String(128), nullable=False)
    """应用 AgentId（从回调 XML AgentID 解析），发送回复时用；为空则用 WecomConfig.agent_id。"""
    agent_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    msg_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)  # pending, replied, failed
    reply_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TwilioPendingMessage(Base):
    """Twilio WhatsApp 入站先入队；本机 lobster_online 轮询拉取后 AI 回复，再通过 submit-reply 由云端代发。"""

    __tablename__ = "twilio_pending_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    twilio_message_sid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    from_user: Mapped[str] = mapped_column(String(128), nullable=False)
    to_user: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    num_media: Mapped[str] = mapped_column(String(8), nullable=False, default="0")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    reply_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PublishTask(Base):
    __tablename__ = "publish_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    result_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ── 独立计费：算力账号（耗算力时用哪个速推 Token）、充值订单 ────────────────────────

class ConsumptionAccount(Base):
    """算力账号：用户可配置多个，每个可绑定速推 Token；调用能力时用其一，扣主账号积分。"""
    __tablename__ = "consumption_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    sutui_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class RechargeOrder(Base):
    """自有充值订单：用户购买积分套餐，支付完成后加积分。"""
    __tablename__ = "recharge_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    amount_yuan: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_fen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 不足1元时用分，如 1分=1
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)  # pending, paid, cancelled
    out_trade_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    payment_method: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    # 审计：微信回调中的实付金额(分)、微信交易号，用于校验与对账
    callback_amount_fen: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    wechat_transaction_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)


class UserInstallation(Base):
    """在线版：同一账号最多绑定 3 个安装身份（installation_id），LRU 顶掉最久未访问。"""

    __tablename__ = "user_installations"
    __table_args__ = (UniqueConstraint("user_id", "installation_id", name="uq_user_installation"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    installation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class InstallationSignupBonusClaim(Base):
    """在线独立认证：每个 installation_id 仅首名注册用户可获得新人积分（防同机多号刷分）。"""

    __tablename__ = "installation_signup_bonus_claims"

    installation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SkillUnlock(Base):
    """用户已付费解锁的技能包。"""
    __tablename__ = "skill_unlocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    package_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    unlocked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SkillUnlockOrder(Base):
    """技能解锁订单：支付完成后写入 SkillUnlock 并下发技能。"""
    __tablename__ = "skill_unlock_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    package_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    amount_yuan: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)  # pending, paid, cancelled
    out_trade_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class BillingIdempotency(Base):
    """预扣幂等：同一用户同一 X-Billing-Idempotency-Key 在窗口内只扣一次，避免双通道重复 pre_deduct。"""

    __tablename__ = "billing_idempotency"
    __table_args__ = (UniqueConstraint("user_id", "key", "endpoint", name="uq_billing_idempotency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(32), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class CreditLedger(Base):
    """积分流水：预扣、结算（实扣/多退少补）、退款、充值、技能解锁、对话扣费等每次变动一行。"""

    __tablename__ = "credit_ledger"
    __table_args__ = (Index("ix_credit_ledger_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    delta: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    # entry_type: pre_deduct | settle | refund | recharge | skill_unlock | sutui_chat | publish_refund | unit_deduct
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    ref_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
