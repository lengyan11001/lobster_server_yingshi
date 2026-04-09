import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

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
from .api.sutui_llm import router as sutui_llm_router
from .api.sutui_chat_proxy import router as sutui_chat_proxy_router
from .api.mcp_gateway import router as mcp_gateway_router
# 自定义配置已迁至客户端；openclaw_config 保留（含 sutui/balance、recharge 等支付）
# from .api.custom_config import router as custom_config_router
from .api.openclaw_config import router as openclaw_config_router
from .api.billing import router as billing_router
# 算力账号已去掉：速推统一走服务器配置的 SUTUI_SERVER_TOKEN(S)，负载均衡
# from .api.consumption_accounts import router as consumption_accounts_router
from .api.mcp_registry import router as mcp_registry_router
# 发布/列表等主要在客户端；服务器须保留 assets（upload-temp + /api/assets/temp/*），供本机无 TOS 时中转公网 URL
from .api.assets import router as assets_router
# from .api.publish import router as publish_router
from .api.logs_api import router as logs_router
from .api.wechat_oa import router as wechat_oa_router
from .api.messenger import router as messenger_router
from .api.twilio_whatsapp import router as twilio_whatsapp_router
from .api.privacy_policy import router as privacy_policy_router
from .api.oauth_public_pages import router as oauth_public_pages_router
from .api.meta_social_publish import router as meta_social_publish_router
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
from .services.sutui_llm_probe import is_sutui_llm_probe_enabled_for_this_instance, sutui_llm_probe_loop_forever
from .services.sutui_reconcile import is_sutui_reconcile_enabled, sutui_reconcile_loop_forever
from .services.meta_social_schedule_runner import meta_social_schedule_background_loop

logger = logging.getLogger(__name__)


def _ensure_default_user():
    """在线版不创建默认用户，仅通过注册或速推扫码登录。"""
    return


def _migrate_capability_configs_extra_config():
    """Add extra_config JSON column to capability_configs if missing."""
    from sqlalchemy import text

    try:
        if "sqlite" not in settings.database_url:
            return
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(capability_configs)"))
            cols = [row[1] for row in r]
            if "extra_config" not in cols:
                conn.execute(text("ALTER TABLE capability_configs ADD COLUMN extra_config JSON"))
                conn.commit()
    except Exception as e:
        logger.warning("Migration capability_configs.extra_config skipped: %s", e)


def _migrate_meta_social_app_credentials():
    """Add per-user meta_app_id / meta_app_secret to meta_social_accounts if missing."""
    from sqlalchemy import text, inspect as sa_inspect

    try:
        insp = sa_inspect(engine)
        if not insp.has_table("meta_social_accounts"):
            return
        cols = [c["name"] for c in insp.get_columns("meta_social_accounts")]
        with engine.begin() as conn:
            if "meta_app_id" not in cols:
                conn.execute(text("ALTER TABLE meta_social_accounts ADD COLUMN meta_app_id VARCHAR(128)"))
            if "meta_app_secret" not in cols:
                conn.execute(text("ALTER TABLE meta_social_accounts ADD COLUMN meta_app_secret TEXT"))
    except Exception as e:
        logger.warning("Migration meta_social_accounts app credentials skipped: %s", e)


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
                    extra_config=None,
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


def _upsert_missing_capabilities_from_catalog():
    """库若在早期只种了部分能力（如仅有 image.generate），补全 catalog 里缺行，避免 pre-deduct 判 cap 为空走 unit_credits=0。"""
    catalog_path = Path(__file__).resolve().parent.parent.parent / "mcp" / "capability_catalog.json"
    if not catalog_path.exists():
        return
    db = SessionLocal()
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        added = 0
        for capability_id, cfg in raw.items():
            if not isinstance(capability_id, str) or not isinstance(cfg, dict):
                continue
            cid = capability_id.strip()
            if db.query(models.CapabilityConfig).filter(models.CapabilityConfig.capability_id == cid).first():
                continue
            db.add(
                models.CapabilityConfig(
                    capability_id=cid,
                    description=str(cfg.get("description") or capability_id),
                    upstream=str(cfg.get("upstream") or "sutui"),
                    upstream_tool=str(cfg.get("upstream_tool") or "").strip(),
                    arg_schema=cfg.get("arg_schema") if isinstance(cfg.get("arg_schema"), dict) else None,
                    extra_config=None,
                    enabled=bool(cfg.get("enabled", True)),
                    is_default=bool(cfg.get("is_default", False)),
                    unit_credits=int(cfg.get("unit_credits") or 0),
                )
            )
            added += 1
        if added:
            db.commit()
            logger.info("Capability catalog: inserted %d missing row(s) from mcp/capability_catalog.json", added)
        else:
            db.rollback()
    except Exception as e:
        db.rollback()
        logger.warning("upsert_missing_capabilities_from_catalog skipped: %s", e)
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
                "【本机 API 服务器】未带 OpenClaw（无 node/openclaw.mjs），此处不启动 — 属正常。"
                "在线版 OpenClaw 必须在用户本机 lobster_online 完整包内运行（见文档「对话走本机」）；"
                "本服务仅提供鉴权/积分等，对话若打到本机则走直连 LLM + MCP(8001)。"
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


def _migrate_user_brand_mark():
    """Add brand_mark column to users if missing（注册时写入品牌标记）。"""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if not insp.has_table("users"):
            return
        cols = [c["name"] for c in insp.get_columns("users")]
        if "brand_mark" in cols:
            return
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN brand_mark VARCHAR(64) NULL"))
    except Exception as e:
        logger.warning("Migration user brand_mark skipped: %s", e)


def _migrate_user_wecom_userid():
    """Add wecom_userid to users（企业微信 FromUserName 绑定，渠道消息按该用户扣费）。"""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if not insp.has_table("users"):
            return
        cols = [c["name"] for c in insp.get_columns("users")]
        if "wecom_userid" in cols:
            return
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN wecom_userid VARCHAR(128) NULL"))
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_wecom_userid ON users (wecom_userid)"))
        except Exception as e:
            logger.debug("ix_users_wecom_userid: %s", e)
    except Exception as e:
        logger.warning("Migration user wecom_userid skipped: %s", e)


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


def _migrate_credits_decimal_sqlite():
    """
    INTEGER 积分列改为 NUMERIC(20,4)。
    SQLite < 3.35 不支持 DROP COLUMN，此前若已执行过「ADD 新列 + DROP 旧列」会失败，
    会出现 users 同时存在 credits 与 credits_d 等半迁移状态；此处统一用表重建修复。
    """
    import sqlite3

    from sqlalchemy import text

    if "sqlite" not in (settings.database_url or "").lower():
        return
    try:
        with engine.begin() as conn:

            def coltypes(table: str) -> dict[str, str]:
                r = conn.execute(text(f"PRAGMA table_info({table})"))
                return {row[1]: (row[2] or "") for row in r.fetchall()}

            def is_int_col(t: str) -> bool:
                return "INT" in (t or "").upper()

            def table_exists(name: str) -> bool:
                r = conn.execute(
                    text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
                    {"n": name},
                )
                return r.fetchone() is not None

            ver = sqlite3.sqlite_version_info
            if ver >= (3, 35, 0):
                _migrate_credits_decimal_sqlite_drop_column(conn, coltypes, is_int_col, table_exists)
            else:
                logger.info(
                    "SQLite %s：不支持 ALTER DROP COLUMN（需 3.35+），使用表重建迁移积分小数列",
                    sqlite3.sqlite_version,
                )
                _migrate_credits_decimal_sqlite_rebuild(conn, coltypes, is_int_col, table_exists)
    except Exception as e:
        logger.warning("Migration credits decimal (sqlite) skipped: %s", e)


def _migrate_credits_decimal_sqlite_drop_column(conn, coltypes, is_int_col, table_exists):
    """SQLite 3.35+：原地改列类型。"""
    from sqlalchemy import text

    ucols = coltypes("users")
    if "credits" in ucols and is_int_col(ucols["credits"]):
        if "credits_d" not in ucols:
            conn.execute(text("ALTER TABLE users ADD COLUMN credits_d NUMERIC(20,4)"))
            conn.execute(text("UPDATE users SET credits_d = CAST(credits AS REAL)"))
        else:
            conn.execute(text("UPDATE users SET credits_d = COALESCE(credits_d, CAST(credits AS REAL))"))
        conn.execute(text("ALTER TABLE users DROP COLUMN credits"))
        conn.execute(text("ALTER TABLE users RENAME COLUMN credits_d TO credits"))

    lcols = coltypes("credit_ledger")
    if "delta" in lcols and is_int_col(lcols["delta"]):
        conn.execute(text("ALTER TABLE credit_ledger ADD COLUMN delta_d NUMERIC(20,4)"))
        conn.execute(text("UPDATE credit_ledger SET delta_d = delta"))
        conn.execute(text("ALTER TABLE credit_ledger DROP COLUMN delta"))
        conn.execute(text("ALTER TABLE credit_ledger RENAME COLUMN delta_d TO delta"))
    lcols = coltypes("credit_ledger")
    if "balance_after" in lcols and is_int_col(lcols["balance_after"]):
        conn.execute(text("ALTER TABLE credit_ledger ADD COLUMN balance_after_d NUMERIC(20,4)"))
        conn.execute(text("UPDATE credit_ledger SET balance_after_d = balance_after"))
        conn.execute(text("ALTER TABLE credit_ledger DROP COLUMN balance_after"))
        conn.execute(text("ALTER TABLE credit_ledger RENAME COLUMN balance_after_d TO balance_after"))

    if table_exists("capability_call_logs"):
        ccols = coltypes("capability_call_logs")
        if "credits_charged" in ccols and is_int_col(ccols["credits_charged"]):
            conn.execute(text("ALTER TABLE capability_call_logs ADD COLUMN credits_charged_d NUMERIC(20,4) DEFAULT 0"))
            conn.execute(text("UPDATE capability_call_logs SET credits_charged_d = credits_charged"))
            conn.execute(text("ALTER TABLE capability_call_logs DROP COLUMN credits_charged"))
            conn.execute(text("ALTER TABLE capability_call_logs RENAME COLUMN credits_charged_d TO credits_charged"))


def _migrate_credits_decimal_sqlite_rebuild(conn, coltypes, is_int_col, table_exists):
    """SQLite 3.34 及以下：表重建；合并 users.credits + users.credits_d（半迁移残留）。"""
    from sqlalchemy import text

    if table_exists("users"):
        ucols = coltypes("users")
        needs_users = ("credits" in ucols and is_int_col(ucols["credits"])) or "credits_d" in ucols
        if needs_users:
            has_cd = "credits_d" in ucols
            has_int_credits = "credits" in ucols and is_int_col(ucols["credits"])
            if has_cd and has_int_credits:
                cred_sql = "COALESCE(CAST(credits_d AS REAL), CAST(credits AS REAL))"
            elif has_cd:
                cred_sql = "CAST(credits_d AS REAL)"
            else:
                cred_sql = "CAST(credits AS REAL)"
            conn.execute(
                text(
                    f"""
                    CREATE TABLE users_mig (
                        id INTEGER NOT NULL PRIMARY KEY,
                        email VARCHAR(255) NOT NULL,
                        hashed_password VARCHAR(255) NOT NULL,
                        credits NUMERIC(20,4) NOT NULL DEFAULT 99999.0000,
                        role VARCHAR(32) NOT NULL,
                        preferred_model VARCHAR(128) NOT NULL,
                        created_at DATETIME NOT NULL,
                        sutui_token TEXT,
                        wechat_openid VARCHAR(64)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    f"""
                    INSERT INTO users_mig
                    SELECT id, email, hashed_password, {cred_sql}, role, preferred_model, created_at, sutui_token, wechat_openid
                    FROM users
                    """
                )
            )
            conn.execute(text("DROP TABLE users"))
            conn.execute(text("ALTER TABLE users_mig RENAME TO users"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_wechat_openid ON users (wechat_openid)"))

    if table_exists("credit_ledger"):
        lcols = coltypes("credit_ledger")
        if "delta" in lcols and is_int_col(lcols["delta"]):
            conn.execute(
                text(
                    """
                    CREATE TABLE credit_ledger_mig (
                        id INTEGER NOT NULL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        delta NUMERIC(20,4) NOT NULL,
                        balance_after NUMERIC(20,4) NOT NULL,
                        entry_type VARCHAR(32) NOT NULL,
                        description VARCHAR(512),
                        ref_type VARCHAR(32),
                        ref_id VARCHAR(128),
                        meta JSON,
                        created_at DATETIME NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO credit_ledger_mig
                    SELECT id, user_id, CAST(delta AS REAL), CAST(balance_after AS REAL),
                           entry_type, description, ref_type, ref_id, meta, created_at
                    FROM credit_ledger
                    """
                )
            )
            conn.execute(text("DROP TABLE credit_ledger"))
            conn.execute(text("ALTER TABLE credit_ledger_mig RENAME TO credit_ledger"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_credit_ledger_user_created ON credit_ledger (user_id, created_at)"
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_credit_ledger_entry_type ON credit_ledger (entry_type)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_credit_ledger_user_id ON credit_ledger (user_id)"))

    if table_exists("capability_call_logs"):
        ccols = coltypes("capability_call_logs")
        if "credits_charged" in ccols and is_int_col(ccols["credits_charged"]):
            conn.execute(
                text(
                    """
                    CREATE TABLE capability_call_logs_mig (
                        id INTEGER NOT NULL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        capability_id VARCHAR(128) NOT NULL,
                        upstream VARCHAR(64),
                        upstream_tool VARCHAR(128),
                        success BOOLEAN NOT NULL,
                        credits_charged NUMERIC(20,4) NOT NULL DEFAULT 0,
                        latency_ms INTEGER,
                        request_payload JSON,
                        response_payload JSON,
                        error_message TEXT,
                        source VARCHAR(64),
                        chat_session_id VARCHAR(128),
                        chat_context_id VARCHAR(128),
                        created_at DATETIME NOT NULL,
                        status VARCHAR(32)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO capability_call_logs_mig
                    SELECT id, user_id, capability_id, upstream, upstream_tool, success,
                           CAST(credits_charged AS REAL), latency_ms, request_payload, response_payload,
                           error_message, source, chat_session_id, chat_context_id, created_at, status
                    FROM capability_call_logs
                    """
                )
            )
            conn.execute(text("DROP TABLE capability_call_logs"))
            conn.execute(text("ALTER TABLE capability_call_logs_mig RENAME TO capability_call_logs"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_capability_call_logs_capability_id ON capability_call_logs (capability_id)"
                )
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_capability_call_logs_user_id ON capability_call_logs (user_id)")
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_capability_call_logs_chat_session_id ON capability_call_logs (chat_session_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_capability_call_logs_chat_context_id ON capability_call_logs (chat_context_id)"
                )
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_capability_call_logs_status ON capability_call_logs (status)")
            )


def _migrate_credits_decimal_mysql():
    from sqlalchemy import text

    url = (settings.database_url or "").lower()
    if "mysql" not in url and "mariadb" not in url:
        return
    stmts = [
        "ALTER TABLE users MODIFY COLUMN credits DECIMAL(20,4) NOT NULL DEFAULT 99999.0000",
        "ALTER TABLE credit_ledger MODIFY COLUMN delta DECIMAL(20,4) NOT NULL",
        "ALTER TABLE credit_ledger MODIFY COLUMN balance_after DECIMAL(20,4) NOT NULL",
        "ALTER TABLE capability_call_logs MODIFY COLUMN credits_charged DECIMAL(20,4) NOT NULL DEFAULT 0.0000",
    ]
    try:
        with engine.begin() as conn:
            for s in stmts:
                try:
                    conn.execute(text(s))
                except Exception as ex:
                    logger.warning("mysql migrate credits: %s err=%s", s[:80], ex)
    except Exception as e:
        logger.warning("Migration credits decimal (mysql) skipped: %s", e)


def _migrate_sutui_recon_balance_remote_prev():
    """为 sutui_reconciliation_runs 增加 balance_remote_prev（上次速推余额），便于 SQL 直接对账。"""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if not insp.has_table("sutui_reconciliation_runs"):
            return
        cols = [c["name"] for c in insp.get_columns("sutui_reconciliation_runs")]
        if "balance_remote_prev" in cols:
            return
        with engine.begin() as conn:
            dname = (conn.dialect.name or "").lower()
            if dname == "sqlite":
                conn.execute(
                    text("ALTER TABLE sutui_reconciliation_runs ADD COLUMN balance_remote_prev NUMERIC(20, 4)")
                )
            else:
                conn.execute(
                    text(
                        "ALTER TABLE sutui_reconciliation_runs ADD COLUMN balance_remote_prev DECIMAL(20,4) NULL"
                    )
                )
        logger.info("[启动] sutui_reconciliation_runs 已增加列 balance_remote_prev")
    except Exception as e:
        logger.warning("Migration sutui_recon balance_remote_prev skipped: %s", e)


def _backfill_installation_signup_bonus_claims():
    """已有 user_installations 的设备视为已占用新人礼包，避免上线后同机多号再领满额分。"""
    from sqlalchemy import inspect

    from . import models
    from .db import SessionLocal

    try:
        insp = inspect(engine)
        if not insp.has_table("installation_signup_bonus_claims") or not insp.has_table("user_installations"):
            return
        db = SessionLocal()
        try:
            if db.query(models.InstallationSignupBonusClaim).count() > 0:
                return
            distinct_iids = [r[0] for r in db.query(models.UserInstallation.installation_id).distinct().all()]
            if not distinct_iids:
                return
            for iid in distinct_iids:
                first = (
                    db.query(models.UserInstallation)
                    .filter(models.UserInstallation.installation_id == iid)
                    .order_by(models.UserInstallation.created_at.asc(), models.UserInstallation.user_id.asc())
                    .first()
                )
                if first is not None:
                    db.add(
                        models.InstallationSignupBonusClaim(
                            installation_id=first.installation_id,
                            user_id=first.user_id,
                            created_at=first.created_at,
                        )
                    )
            db.commit()
            logger.info(
                "[启动] installation_signup_bonus_claims 已从 user_installations 回填 %s 条",
                len(distinct_iids),
            )
        except Exception as e:
            db.rollback()
            logger.warning("Backfill installation_signup_bonus_claims failed: %s", e)
        finally:
            db.close()
    except Exception as e:
        logger.warning("Backfill installation_signup_bonus_claims skipped: %s", e)


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    bg_tasks: List[asyncio.Task] = []
    if is_sutui_llm_probe_enabled_for_this_instance():
        bg_tasks.append(asyncio.create_task(sutui_llm_probe_loop_forever(3600.0)))
    else:
        logger.info(
            "[启动] 速推 LLM 定时探测已关闭（海外实例：LOBSTER_SERVER_REGION=overseas，"
            "或已设置 LOBSTER_SUTUI_LLM_PROBE_ENABLED=0）"
        )
    if is_sutui_reconcile_enabled():
        bg_tasks.append(asyncio.create_task(sutui_reconcile_loop_forever(3600.0)))
    else:
        logger.info("[启动] 速推对账任务已关闭（海外实例或 LOBSTER_SUTUI_RECONCILE_ENABLED=0）")
    bg_tasks.append(asyncio.create_task(meta_social_schedule_background_loop()))
    logger.info("[启动] Meta Social 定时发布后台已启动")
    yield
    for t in bg_tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    logger.info("[启动] create_app 开始")
    Base.metadata.create_all(bind=engine)
    _migrate_user_sutui_token()
    _migrate_user_wechat_openid()
    _migrate_user_brand_mark()
    _migrate_user_wecom_userid()
    _migrate_wecom_config_secret()
    _migrate_wecom_agent_id()
    _migrate_recharge_amount_fen()
    _migrate_recharge_callback_audit()
    _migrate_credits_decimal_sqlite()
    _migrate_credits_decimal_mysql()
    _backfill_installation_signup_bonus_claims()
    _migrate_sutui_recon_balance_remote_prev()
    _migrate_capability_configs_extra_config()
    _migrate_meta_social_app_credentials()
    _ensure_default_user()
    _seed_capability_catalog()
    _upsert_missing_capabilities_from_catalog()
    _auto_start_openclaw()

    app = FastAPI(
        title="龙虾 (Lobster) API",
        version="0.1.0",
        description="龙虾 - 你的私人 AI 助手",
        lifespan=_app_lifespan,
    )

    # 规范禁止 ACAO=* 与 Access-Control-Allow-Credentials:true 同时出现；浏览器会拒收响应。
    # 本服务鉴权为 Bearer JWT（非跨站 Cookie），无需 credentials=True；否则与 allow_origins=["*"] 组合会踩坑。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list(),
        allow_credentials=False,
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
    app.include_router(privacy_policy_router, prefix="")
    app.include_router(oauth_public_pages_router, prefix="")
    app.include_router(auth_router, prefix="/auth")
    app.include_router(capabilities_router, prefix="")
    app.include_router(skills_router, prefix="")
    app.include_router(settings_router, prefix="")
    app.include_router(sutui_llm_router, prefix="")
    app.include_router(sutui_chat_proxy_router, prefix="")
    app.include_router(chat_router, prefix="")
    app.include_router(mcp_gateway_router, prefix="")
    app.include_router(openclaw_config_router, prefix="")
    # 自定义配置已迁至客户端；server 仅保留支付相关（sutui/balance、recharge 在 openclaw_config 中）
    # app.include_router(custom_config_router, prefix="")
    app.include_router(billing_router, prefix="")
    # app.include_router(consumption_accounts_router, prefix="")
    app.include_router(mcp_registry_router, prefix="")
    app.include_router(assets_router, prefix="")
    # app.include_router(publish_router, prefix="")
    app.include_router(logs_router, prefix="")
    app.include_router(wechat_oa_router, prefix="")
    app.include_router(messenger_router, prefix="")
    app.include_router(twilio_whatsapp_router, prefix="")
    app.include_router(meta_social_publish_router, prefix="")
    if wecom_router is not None:
        app.include_router(wecom_router, prefix="")
    else:
        logger.warning("企业微信回复未加载：缺少 pycryptodome 或 skills.wecom_reply")

    assets_dir = Path(__file__).resolve().parent.parent.parent / "assets"
    assets_dir.mkdir(exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(assets_dir)), name="media")

    # 在线版客户端技能包 manifest / zip（HTTPS 直链，无需登录；与 lobster_online SKILL_BUNDLE_MANIFEST_URL 对应）
    _skill_bundle_dir = Path(__file__).resolve().parent.parent.parent / "client_static" / "skill_bundle"
    _skill_bundle_dir.mkdir(parents=True, exist_ok=True)
    (_skill_bundle_dir / "bundles").mkdir(exist_ok=True)
    app.mount(
        "/client/skill-bundle",
        StaticFiles(directory=str(_skill_bundle_dir)),
        name="client_skill_bundle",
    )

    # 在线版客户端「纯代码包」manifest / zip（与 lobster_online CLIENT_CODE_MANIFEST_URL 对应）
    _client_code_dir = Path(__file__).resolve().parent.parent.parent / "client_static" / "client_code"
    _client_code_dir.mkdir(parents=True, exist_ok=True)
    (_client_code_dir / "bundles").mkdir(exist_ok=True)
    app.mount(
        "/client/client-code",
        StaticFiles(directory=str(_client_code_dir)),
        name="client_client_code",
    )

    # 前端由 lobster_online 提供，本服务仅 API；根路径返回说明
    @app.get("/", include_in_schema=False)
    def index():
        return JSONResponse(content={"message": "Lobster API. Use the online client (lobster_online) to access the UI."})

    logger.info("[启动] create_app 完成")
    return app


app = create_app()
logger.info("[启动] Lobster API 已加载")
