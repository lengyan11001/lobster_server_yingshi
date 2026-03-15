import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.health import router as health_router
from .api.auth import router as auth_router, get_password_hash
from .api.chat import router as chat_router
from .api.capabilities import router as capabilities_router
from .api.skills import router as skills_router
from .api.settings_api import router as settings_router
from .api.mcp_gateway import router as mcp_gateway_router
from .api.openclaw_config import router as openclaw_config_router
from .api.custom_config import router as custom_config_router
from .api.billing import router as billing_router
from .api.consumption_accounts import router as consumption_accounts_router
from .api.mcp_registry import router as mcp_registry_router
from .api.assets import router as assets_router
from .api.publish import router as publish_router
from .api.logs_api import router as logs_router
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
    """Start OpenClaw Gateway if it's not already running."""
    try:
        from .api.openclaw_config import _find_openclaw_pid, _restart_openclaw_gateway
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


def create_app() -> FastAPI:
    logger.info("[启动] create_app 开始")
    Base.metadata.create_all(bind=engine)
    _migrate_user_sutui_token()
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
    app.include_router(custom_config_router, prefix="")
    app.include_router(billing_router, prefix="")
    app.include_router(consumption_accounts_router, prefix="")
    app.include_router(mcp_registry_router, prefix="")
    app.include_router(assets_router, prefix="")
    app.include_router(publish_router, prefix="")
    app.include_router(logs_router, prefix="")

    assets_dir = Path(__file__).resolve().parent.parent.parent / "assets"
    assets_dir.mkdir(exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(assets_dir)), name="media")

    static_dir = Path(__file__).resolve().parent.parent.parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(static_dir / "index.html")

    logger.info("[启动] create_app 完成")
    return app


app = create_app()
logger.info("[启动] Lobster API 已加载")
