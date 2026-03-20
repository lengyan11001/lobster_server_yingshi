import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.health import router as health_router
from .api.auth import router as auth_router, get_password_hash
from .api.chat import router as chat_router
from .api.capabilities import router as capabilities_router
from .api.skills import router as skills_router
from .api.settings_api import router as settings_router
from .api.mcp_gateway import router as mcp_gateway_router
# 自定义配置已迁至客户端；openclaw_config 保留（含 sutui/balance、recharge 等支付）
# from .api.custom_config import router as custom_config_router
from .api.openclaw_config import router as openclaw_config_router
from .api.billing import router as billing_router
# 算力账号已去掉：速推统一走服务器配置的 SUTUI_SERVER_TOKEN(S)，负载均衡
# from .api.consumption_accounts import router as consumption_accounts_router
from .api.mcp_registry import router as mcp_registry_router
# 发布账号/任务、素材：已迁至客户端（lobster_online），server 不再提供
# from .api.assets import router as assets_router
# from .api.publish import router as publish_router
from .api.logs_api import router as logs_router
from .api.wechat_oa import router as wechat_oa_router
try:
    from .api.wecom import router as wecom_router
except Exception as e:
    if "Crypto" in str(e) or "pycryptodome" in str(e).lower() or "wecom_reply" in str(e):
        wecom_router = None
    else:
        raise
from .core.config import settings
from .db import Base, engine, SessionLocal
from . import models  # noqa: F401

logger = logging.getLogger(__name__)


def _ensure_default_user():
    """在线版不创建默认用户，仅通过注册或速推扫码登录。"""
    return


def _seed_capability_catalog():
    """Import capability catalog from mcp/capability_catalog.json on first run."""
    catalog_path = Path(__file__).resolve().parent.parent.parent / "mcp" / "capability_catalog.json"
    if not catalog_path.exists():
        return
    db = SessionLocal()
    try:
        if db.query(models.CapabilityConfig).count() > 0:
            return
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        for capability_id, cfg in raw.items():
            if not isinstance(capability_id, str) or not isinstance(cfg, dict):
                continue
            db.add(
                models.CapabilityConfig(
                    capability_id=capability_id.strip(),
                    description=str(cfg.get("description") or capability_id),
                    upstream=str(cfg.get("upstream") or "sutui"),
                    upstream_tool=str(cfg.get("upstream_tool") or "").strip(),
                    arg_schema=cfg.get("arg_schema") if isinstance(cfg.get("arg_schema"), dict) else None,
                    enabled=bool(cfg.get("enabled", True)),
                    is_default=bool(cfg.get("is_default", False)),
                    unit_credits=int(cfg.get("unit_credits") or 0),
                )
            )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _auto_start_openclaw():
    """Start OpenClaw Gateway if it's not already running (仅当本机存在 node + openclaw.mjs，与 lobster_online 完整包一致)。"""
    try:
        if not getattr(settings, "openclaw_autostart", True):
            logger.info("OpenClaw 自动启动已关闭（OPENCLAW_AUTOSTART=false）")
            return
        from .api.openclaw_config import (
            _find_openclaw_entry,
            _find_openclaw_pid,
            _restart_openclaw_gateway,
        )
        # 在线版 API 服务器通常不部署 OpenClaw/Node，不应打 WARNING
        if not _find_openclaw_entry():
            logger.info(
                "OpenClaw 未随本服务部署（无 node 或可执行的 openclaw.mjs），跳过自动启动；"
                "对话走直连 LLM，工具与生成能力走 MCP（如 8001）。"
            )
            return
        if not _find_openclaw_pid():
            logger.info("OpenClaw Gateway not detected, auto-starting...")
            ok = _restart_openclaw_gateway()
            if ok:
                logger.info("OpenClaw Gateway auto-started successfully")
            else:
                logger.warning("OpenClaw auto-start failed (chat will use direct LLM API)")
        else:
            logger.info("OpenClaw Gateway already running")
    except Exception as e:
        logger.warning("OpenClaw auto-start skipped: %s", e)


def _migrate_user_sutui_token():
    """Add sutui_token column to users if missing (online edition)."""
    from sqlalchemy import text
    try:
        if "sqlite" not in settings.database_url:
            return
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(users)"))
            cols = [row[1] for row in r]
            if "sutui_token" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN sutui_token TEXT"))
                conn.commit()
    except Exception as e:
        logger.warning("Migration sutui_token skipped: %s", e)


def _migrate_user_wechat_openid():
    """Add wechat_openid column to users if missing (自建微信登录)."""
    from sqlalchemy import text
    try:
        if "sqlite" not in settings.database_url:
            return
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(users)"))
            cols = [row[1] for row in r]
            if "wechat_openid" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN wechat_openid VARCHAR(64)"))
                conn.commit()
    except Exception as e:
        logger.warning("Migration wechat_openid skipped: %s", e)


def _migrate_wecom_config_secret():
    """Add secret column to wecom_configs if missing (用于轮询模式下发送应用消息)."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(wecom_configs)"))
            cols = [row[1] for row in r]
            if "secret" not in cols:
                conn.execute(text("ALTER TABLE wecom_configs ADD COLUMN secret VARCHAR(255)"))
                conn.commit()
    except Exception as e:
        logger.warning("Migration wecom_configs.secret skipped: %s", e)


def _migrate_wecom_agent_id():
    """Add agent_id to wecom_configs and wecom_pending_messages (发送应用消息时必填)."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(wecom_configs)"))
            cols = [row[1] for row in r]
            if "agent_id" not in cols:
                conn.execute(text("ALTER TABLE wecom_configs ADD COLUMN agent_id INTEGER"))
                conn.commit()
            r2 = conn.execute(text("PRAGMA table_info(wecom_pending_messages)"))
            cols2 = [row[1] for row in r2]
            if "agent_id" not in cols2:
                conn.execute(text("ALTER TABLE wecom_pending_messages ADD COLUMN agent_id INTEGER"))
                conn.commit()
    except Exception as e:
        logger.warning("Migration wecom agent_id skipped: %s", e)


def _migrate_recharge_amount_fen():
    """Add amount_fen to recharge_orders（1分钱套餐用分计费）."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(recharge_orders)"))
            cols = [row[1] for row in r]
            if "amount_fen" not in cols:
                conn.execute(text("ALTER TABLE recharge_orders ADD COLUMN amount_fen INTEGER DEFAULT 0"))
                conn.commit()
    except Exception as e:
        logger.warning("Migration recharge_orders.amount_fen skipped: %s", e)


def _migrate_recharge_callback_audit():
    """Add callback_amount_fen, wechat_transaction_id to recharge_orders（回调金额与交易号审计）."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(recharge_orders)"))
            cols = [row[1] for row in r]
            if "callback_amount_fen" not in cols:
                conn.execute(text("ALTER TABLE recharge_orders ADD COLUMN callback_amount_fen INTEGER"))
                conn.commit()
            if "wechat_transaction_id" not in cols:
                conn.execute(text("ALTER TABLE recharge_orders ADD COLUMN wechat_transaction_id VARCHAR(64)"))
                conn.commit()
    except Exception as e:
        logger.warning("Migration recharge_orders callback_audit skipped: %s", e)


def create_app() -> FastAPI:
    logger.info("[启动] create_app 开始")
    Base.metadata.create_all(bind=engine)
    _migrate_user_sutui_token()
    _migrate_user_wechat_openid()
    _migrate_wecom_config_secret()
    _migrate_wecom_agent_id()
    _migrate_recharge_amount_fen()
    _migrate_recharge_callback_audit()
    _ensure_default_user()
    _seed_capability_catalog()
    _auto_start_openclaw()

    app = FastAPI(
        title="龙虾 (Lobster) API",
        version="0.1.0",
        description="龙虾 - 你的私人 AI 助手",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def catch_all(request: Request, exc: Exception):
        if settings.debug:
            import traceback
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal Server Error", "debug": str(exc), "traceback": traceback.format_exc()},
            )
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

    app.include_router(health_router, prefix="")
    app.include_router(auth_router, prefix="/auth")
    app.include_router(capabilities_router, prefix="")
    app.include_router(skills_router, prefix="")
    app.include_router(settings_router, prefix="")
    app.include_router(chat_router, prefix="")
    app.include_router(mcp_gateway_router, prefix="")
    app.include_router(openclaw_config_router, prefix="")
    # 自定义配置已迁至客户端；server 仅保留支付相关（sutui/balance、recharge 在 openclaw_config 中）
    # app.include_router(custom_config_router, prefix="")
    app.include_router(billing_router, prefix="")
    # app.include_router(consumption_accounts_router, prefix="")
    app.include_router(mcp_registry_router, prefix="")
    # app.include_router(assets_router, prefix="")
    # app.include_router(publish_router, prefix="")
    app.include_router(logs_router, prefix="")
    app.include_router(wechat_oa_router, prefix="")
    if wecom_router is not None:
        app.include_router(wecom_router, prefix="")
    else:
        logger.warning("企业微信回复未加载：缺少 pycryptodome 或 skills.wecom_reply")

    assets_dir = Path(__file__).resolve().parent.parent.parent / "assets"
    assets_dir.mkdir(exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(assets_dir)), name="media")

    # 前端由 lobster_online 提供，本服务仅 API；根路径返回说明
    @app.get("/", include_in_schema=False)
    def index():
        return JSONResponse(content={"message": "Lobster API. Use the online client (lobster_online) to access the UI."})

    logger.info("[启动] create_app 完成")
    return app


app = create_app()
logger.info("[启动] Lobster API 已加载")
