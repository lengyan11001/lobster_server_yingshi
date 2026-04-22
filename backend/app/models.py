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
    """注册时客户端所在安装包的品牌标记（与 LOBSTER_BRAND_MARK / brands.json 的 marks 键一致，默认 yingshi；兼容 bihuo）。"""
    brand_mark: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    """企业微信消息回调 FromUserName（成员 userid 等），与站内账号绑定后用于渠道侧扣费。"""
    wecom_userid: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, unique=True, index=True)
    is_agent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    parent_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
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
    """通讯录同步 Secret，用于获取通讯录 access_token（与应用 secret 不同）。"""
    contacts_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
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


class LandingOrder(Base):
    """落地页匿名订单：访客购买 INSclaw 安装包（无需登录）。
    支付完成后生成 download_token，访客凭 token 在有限时间内访问下载链接。"""

    __tablename__ = "landing_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    out_trade_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # insclaw_full / insclaw_slim
    amount_fen: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)  # pending / paid / cancelled
    payment_method: Mapped[str] = mapped_column(String(32), default="wechat", nullable=False)
    # 联系信息（可选；用户填了便于客服重发链接 / 纸质开票）
    contact_email: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    contact_phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # 审计
    client_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # 支付完成后的实付 + 微信交易号
    callback_amount_fen: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    wechat_transaction_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # 下载凭证：paid 时生成，过期时间默认 7 天，最大下载次数 10
    download_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True, index=True)
    download_token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


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


class UserSkillVisibility(Base):
    """用户可见技能：每行 = 该用户可在技能商店看到的一个 package_id。管理员可增删，新用户自动种子默认列表。"""

    __tablename__ = "user_skill_visibility"
    __table_args__ = (UniqueConstraint("user_id", "package_id", name="uq_user_skill_visibility"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    package_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
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


# ── Meta Social（Instagram / Facebook）发布 + 数据同步 ────────────────────────


class MetaSocialAccount(Base):
    """Meta 社交账号：一条 = 一个 Facebook 主页（及其关联的 Instagram Business 账号）。"""

    __tablename__ = "meta_social_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "facebook_page_id", name="uq_meta_social_user_page"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), default="", nullable=False)

    # ── Facebook 主页 ──
    facebook_page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    facebook_page_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    page_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # ── Instagram Business（关联到该主页）──
    instagram_business_account_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    instagram_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # ── Facebook App 凭据（per-user，OAuth 授权时用户自行填写）──
    meta_app_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    meta_app_secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── 代理（防风控，每账号独立）──
    proxy_server: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    proxy_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    proxy_password: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # ── 元数据 ──
    scopes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    meta_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SocialPublishSchedule(Base):
    """IG / FB 定时发布队列：每条绑定一个 MetaSocialAccount + 平台，从 asset_ids_json 先进先出。"""

    __tablename__ = "social_publish_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    meta_account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)  # instagram / facebook
    content_type: Mapped[str] = mapped_column(String(32), default="photo", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    asset_ids_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    privacy_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_run_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_post_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SocialContentSnapshot(Base):
    """IG / FB 数据快照：每次同步写入一行，供 LLM 查询工具读取。"""

    __tablename__ = "social_content_snapshots"
    __table_args__ = (Index("ix_social_snap_user_account", "user_id", "meta_account_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    meta_account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)  # instagram / facebook
    items: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    account_insights: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class SutuiReconciliationRun(Base):
    """定时对账：每把速推 server token 的远端余额变动 vs 本地带 _recon 的积分流水（运营用，不对用户展示）。

    字段含义（同一 sutui_token_ref 按 id 链式相邻两行对比）：
    - balance_remote_prev：上一轮入库的速推余额（「上次余额」）；基线行无
    - balance_remote：本轮拉取的速推余额（「本次余额」）
    - remote_delta：.prev − .current = 速推侧消耗（正数表示余额减少）
    - local_net_credits：自上一轮 created_at 起，本地 credit_ledger 中带 _recon 且 ref 匹配的净消耗（与用户侧扣费口径一致）
    - diff：remote_delta − local_net_credits（误差；|diff| 大需排查）
    """

    __tablename__ = "sutui_reconciliation_runs"
    __table_args__ = (Index("ix_sutui_recon_ref_created", "sutui_token_ref", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    pool: Mapped[str] = mapped_column(String(32), nullable=False)
    sutui_token_ref: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    balance_remote_prev: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True)
    balance_remote: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True)
    remote_delta: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True)
    local_net_credits: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True)
    diff: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
