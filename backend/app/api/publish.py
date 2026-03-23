"""Publishing accounts and task management. 抖音发布支持两种扣费：按次扣积分（10/次）或解锁后免费。"""
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import get_current_user
from .installation_slots import ensure_installation_slot, installation_slots_enabled, parse_installation_id_strict
from ..db import get_db
from ..models import Asset, CapabilityCallLog, PublishAccount, PublishTask, SkillUnlock, User

logger = logging.getLogger(__name__)
router = APIRouter()

# 添加更多发布账号：未解锁时仅可添加 1 个账号，解锁后无限制
ADD_MORE_PUBLISH_ACCOUNTS_PACKAGE_ID = "add_more_publish_accounts"

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
BROWSER_DATA_DIR = _BASE_DIR / "browser_data"
BROWSER_DATA_DIR.mkdir(exist_ok=True)

SUPPORTED_PLATFORMS = {
    "douyin": {"name": "抖音", "login_url": "https://creator.douyin.com"},
    "bilibili": {"name": "B站", "login_url": "https://member.bilibili.com"},
    "xiaohongshu": {"name": "小红书", "login_url": "https://creator.xiaohongshu.com"},
    "kuaishou": {"name": "快手", "login_url": "https://cp.kuaishou.com"},
}

def _ensure_tiny_mp4(path: Path) -> Path:
    # A tiny MP4 (base64) for dry-run uploads.
    import base64
    tiny_b64 = (
        "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAACMbW9vdgAAAGxtdmhk"
        "AAAAAAAAAAAAAAAAAAAAAAAD6AAAA+gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAA"
        "AAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAIVdHJhawAA"
        "AFx0a2hkAAAAAAAAAAAAAAAAAAAAAAABAAAAAAAAA+gAAAAAAAAAAAAAAAAEAAAAA"
        "AAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAIAAAABAAAAAQAAAAAAJGVkdHMAAAAc"
        "ZWxzdAAAAAAAAAABAAAD6AAAA+gAAAAAAAEabWRpYQAAACBtZGhkAAAAAAAAAAAA"
        "AAAAAAAAAAAyAAAAMgAAVcQAAAAAAC1oZGxyAAAAAAAAAAB2aWRlAAAAAAAAAAAA"
        "AAAAAFZpZGVvSGFuZGxlcgAAAAE3bWluZgAAABR2bWhkAAAAAAAAAAAAAAAALGRp"
        "bmYAAAAcZHJlZgAAAAAAAAABAAAADHVybCAAAAABAAAAK3N0YmwAAAAVc3RzZAAA"
        "AAEAAAANYXZjMQAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUYXZjQwEB/4QAF2JtZGF0AAAAAA=="
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return path
    path.write_bytes(base64.b64decode(tiny_b64))
    return path


# ── Account CRUD ──────────────────────────────────────────────────

class AddAccountReq(BaseModel):
    platform: str
    nickname: str


@router.get("/api/accounts", summary="列出发布账号")
def list_accounts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    x_installation_id: Optional[str] = Header(None, alias="X-Installation-Id"),
):
    rows = db.query(PublishAccount).filter(
        PublishAccount.user_id == current_user.id,
    ).order_by(PublishAccount.created_at.desc()).all()
    n = len(rows)
    if n < 1:
        can_add_more = True
    elif _user_has_add_more_accounts_unlock(db, current_user.id):
        if installation_slots_enabled():
            iid = parse_installation_id_strict(x_installation_id)
            ensure_installation_slot(db, current_user.id, iid)
        can_add_more = True
    else:
        can_add_more = False
    return {
        "accounts": [
            {
                "id": a.id,
                "platform": a.platform,
                "platform_name": SUPPORTED_PLATFORMS.get(a.platform, {}).get("name", a.platform),
                "nickname": a.nickname,
                "status": a.status,
                "last_login": a.last_login.isoformat() if a.last_login else None,
                "created_at": a.created_at.isoformat() if a.created_at else "",
            }
            for a in rows
        ],
        "platforms": [
            {"id": k, "name": v["name"]} for k, v in SUPPORTED_PLATFORMS.items()
        ],
        "can_add_more": can_add_more,
        "add_more_unlock_package_id": ADD_MORE_PUBLISH_ACCOUNTS_PACKAGE_ID,
        "add_more_unlock_credits": 1000,
    }


@router.post("/api/accounts", summary="添加发布账号")
def add_account(
    body: AddAccountReq,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    x_installation_id: Optional[str] = Header(None, alias="X-Installation-Id"),
):
    if body.platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(400, detail=f"不支持的平台: {body.platform}")

    existing_count = db.query(PublishAccount).filter(PublishAccount.user_id == current_user.id).count()
    if existing_count >= 1 and not _user_has_add_more_accounts_unlock(db, current_user.id):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=402,
            content={
                "detail": "当前仅可添加 1 个发布账号。解锁「添加更多发布账号」后可添加多个，需 1000 积分。",
                "need_credits_unlock": True,
                "package_id": ADD_MORE_PUBLISH_ACCOUNTS_PACKAGE_ID,
                "package_name": "添加更多发布账号",
                "amount_credits": 1000,
            },
        )

    if existing_count >= 1 and installation_slots_enabled():
        iid = parse_installation_id_strict(x_installation_id)
        ensure_installation_slot(db, current_user.id, iid)

    profile_dir = BROWSER_DATA_DIR / f"{body.platform}_{body.nickname}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    acct = PublishAccount(
        user_id=current_user.id,
        platform=body.platform,
        nickname=body.nickname.strip(),
        status="pending",
        browser_profile=str(profile_dir),
    )
    db.add(acct)
    db.commit()
    db.refresh(acct)
    return {
        "id": acct.id,
        "platform": acct.platform,
        "nickname": acct.nickname,
        "status": acct.status,
        "message": f"账号已添加，请点击「登录」完成扫码",
    }


@router.post("/api/accounts/{account_id}/login", summary="启动浏览器登录")
async def start_login(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    acct = db.query(PublishAccount).filter(
        PublishAccount.id == account_id,
        PublishAccount.user_id == current_user.id,
    ).first()
    if not acct:
        raise HTTPException(404, detail="账号不存在")

    platform_info = SUPPORTED_PLATFORMS.get(acct.platform, {})
    login_url = platform_info.get("login_url", "")

    try:
        from publisher.browser_pool import open_login_browser
        _ = await open_login_browser(
            profile_dir=acct.browser_profile or str(BROWSER_DATA_DIR / f"{acct.platform}_{acct.nickname}"),
            login_url=login_url,
            platform=acct.platform,
        )
        # Don't block/poll here; don't pop interruptive messages.
        acct.status = "pending"
        db.commit()
        return {"ok": True, "status": "pending", "message": "已打开浏览器，请扫码登录（完成后手动关闭窗口）"}
    except Exception as e:
        logger.exception("Login browser failed")
        return {"ok": False, "status": "error", "message": str(e)}


@router.post("/api/accounts/{account_id}/open-browser", summary="打开账号浏览器")
async def open_browser(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    acct = db.query(PublishAccount).filter(
        PublishAccount.id == account_id,
        PublishAccount.user_id == current_user.id,
    ).first()
    if not acct:
        raise HTTPException(404, detail="账号不存在")

    platform_info = SUPPORTED_PLATFORMS.get(acct.platform, {})
    login_url = platform_info.get("login_url", "")
    profile_dir = acct.browser_profile or str(BROWSER_DATA_DIR / f"{acct.platform}_{acct.nickname}")

    try:
        from publisher.browser_pool import open_and_check_browser
        result = await open_and_check_browser(
            profile_dir=profile_dir,
            login_url=login_url,
            platform=acct.platform,
        )
        logged_in = result.get("logged_in", False)
        if logged_in and acct.status != "active":
            acct.status = "active"
            acct.last_login = datetime.utcnow()
            db.commit()
        return {"ok": True, "logged_in": logged_in, "message": result.get("message", "")}
    except Exception as e:
        logger.exception("Open browser failed")
        return {"ok": False, "logged_in": False, "message": str(e)}


@router.get("/api/accounts/{account_id}/login-status", summary="检查登录状态")
async def check_login_status(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    acct = db.query(PublishAccount).filter(
        PublishAccount.id == account_id,
        PublishAccount.user_id == current_user.id,
    ).first()
    if not acct:
        raise HTTPException(404, detail="账号不存在")

    try:
        from publisher.browser_pool import check_browser_login
        logged_in = await check_browser_login(
            profile_dir=acct.browser_profile or str(BROWSER_DATA_DIR / f"{acct.platform}_{acct.nickname}"),
            platform=acct.platform,
        )
        if logged_in and acct.status != "active":
            acct.status = "active"
            acct.last_login = datetime.utcnow()
            db.commit()
        return {"logged_in": logged_in, "message": "已登录" if logged_in else "未登录，请在浏览器中扫码"}
    except Exception as e:
        logger.exception("Check login status failed")
        return {"logged_in": False, "message": str(e)}


@router.delete("/api/accounts/{account_id}", summary="删除发布账号")
def delete_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    acct = db.query(PublishAccount).filter(
        PublishAccount.id == account_id,
        PublishAccount.user_id == current_user.id,
    ).first()
    if not acct:
        raise HTTPException(404, detail="账号不存在")
    import shutil
    if acct.browser_profile:
        p = Path(acct.browser_profile)
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    db.delete(acct)
    db.commit()
    return {"ok": True}


# ── Publish tasks ─────────────────────────────────────────────────

class PublishReq(BaseModel):
    asset_id: str
    account_id: Optional[int] = None
    account_nickname: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None
    # platform-specific options, e.g. douyin schedule/visibility/location/yellow_cart
    options: Optional[dict] = None
    # optional cover image asset id (if platform supports separate cover upload)
    cover_asset_id: Optional[str] = None


@router.post("/api/douyin/dryrun", summary="抖音发布 dry-run（走到发布前一步）")
async def douyin_dryrun(
    account_nickname: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    acct = db.query(PublishAccount).filter(
        PublishAccount.user_id == current_user.id,
        PublishAccount.platform == "douyin",
        PublishAccount.nickname == account_nickname.strip(),
    ).first()
    if not acct or not acct.browser_profile:
        raise HTTPException(404, detail="抖音账号不存在或未配置浏览器 profile")

    # Generate a tiny local MP4 for upload dry-run
    from .assets import ASSETS_DIR
    mp4_path = _ensure_tiny_mp4(Path(ASSETS_DIR) / "dryrun_tiny.mp4")

    try:
        from publisher.browser_pool import dryrun_douyin_upload_in_context
        result = await dryrun_douyin_upload_in_context(
            profile_dir=acct.browser_profile,
            file_path=str(mp4_path),
        )
        return {"ok": True, "result": result}
    except Exception as e:
        logger.exception("Douyin dryrun failed")
        return {"ok": False, "error": str(e)}


def _user_has_add_more_accounts_unlock(db: Session, user_id: int) -> bool:
    return db.query(SkillUnlock).filter(
        SkillUnlock.user_id == user_id,
        SkillUnlock.package_id == ADD_MORE_PUBLISH_ACCOUNTS_PACKAGE_ID,
    ).first() is not None


@router.post("/api/publish", summary="发布素材到平台")
async def create_publish_task(
    body: PublishReq,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    logger.info("[PUBLISH-API] 请求: asset_id=%s account_nickname=%s", body.asset_id, body.account_nickname)
    asset = db.query(Asset).filter(
        Asset.asset_id == body.asset_id,
        Asset.user_id == current_user.id,
    ).first()
    if not asset:
        raise HTTPException(404, detail=f"素材不存在: {body.asset_id}")

    acct = None
    if body.account_id:
        acct = db.query(PublishAccount).filter(
            PublishAccount.id == body.account_id,
            PublishAccount.user_id == current_user.id,
        ).first()
    elif body.account_nickname:
        acct = db.query(PublishAccount).filter(
            PublishAccount.nickname == body.account_nickname.strip(),
            PublishAccount.user_id == current_user.id,
        ).first()
    if not acct:
        raise HTTPException(404, detail="发布账号不存在，请先在「发布管理」中添加账号")
    # 抖音/小红书默认已安装直接可用，无需在会话中再校验解锁

    credits_charged = 0

    task = PublishTask(
        user_id=current_user.id,
        asset_id=body.asset_id,
        account_id=acct.id,
        title=body.title,
        description=body.description,
        tags=body.tags,
        status="pending",
        meta={
            "options": body.options or {},
            "cover_asset_id": body.cover_asset_id,
            "platform": acct.platform,
            "account_nickname": acct.nickname,
        },
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    try:
        from publisher.browser_pool import run_publish_task
        from .assets import ASSETS_DIR, _asset_local_path

        async def _resolve_asset_path(a: Asset):
            """返回 (path_str, temp_path_to_delete)。仅 TOS 时下载到临时文件。"""
            local = _asset_local_path(a)
            if local is not None:
                return str(local), None
            url = getattr(a, "source_url", None) or ""
            if url.startswith("http://") or url.startswith("https://"):
                async with httpx.AsyncClient(timeout=120.0) as c:
                    r = await c.get(url)
                r.raise_for_status()
                suf = Path(a.filename or "").suffix or ".mp4"
                fd, path = tempfile.mkstemp(suffix=suf)
                try:
                    import os
                    os.write(fd, r.content)
                finally:
                    import os
                    os.close(fd)
                return path, path
            raise HTTPException(400, detail="素材文件不可用（无本地文件且无公网 URL）")

        file_path, temp_video = await _resolve_asset_path(asset)
        logger.info("[PUBLISH-API] asset file=%s exists=%s",
                     file_path, Path(file_path).exists())
        cover_path = None
        temp_cover = None
        if body.cover_asset_id:
            cover = db.query(Asset).filter(
                Asset.asset_id == body.cover_asset_id,
                Asset.user_id == current_user.id,
            ).first()
            if cover:
                cover_path, temp_cover = await _resolve_asset_path(cover)
        logger.info("[PUBLISH-API] calling run_publish_task: platform=%s profile=%s title=%s",
                     acct.platform, acct.browser_profile, body.title)
        result = await run_publish_task(
            profile_dir=acct.browser_profile,
            platform=acct.platform,
            file_path=file_path,
            title=body.title or "",
            description=body.description or "",
            tags=body.tags or "",
            options=body.options or {},
            cover_path=cover_path,
        )
        for p in (temp_video, temp_cover):
            if p and Path(p).exists():
                try:
                    Path(p).unlink()
                except Exception:
                    pass
        logger.info("[PUBLISH-API] result: %s", {k: v for k, v in result.items() if k != "applied"})
        task.status = "success" if result.get("ok") else "failed"
        if result.get("need_login"):
            task.status = "need_login"
        task.result_url = result.get("url", "")
        task.error = result.get("error", "")
        task.meta = {
            **(task.meta or {}),
            "driver_result": result,
        }
        task.finished_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        task.status = "failed"
        task.error = str(e)
        task.finished_at = datetime.utcnow()
        db.commit()
        logger.exception("[PUBLISH-API] publish task exception")

    # 发布失败且已扣积分则退还
    if credits_charged > 0 and task.status != "success":
        db.refresh(current_user)
        current_user.credits = (current_user.credits or 0) + credits_charged
        db.commit()
        credits_charged = 0
    elif credits_charged > 0:
        db.add(CapabilityCallLog(
            user_id=current_user.id,
            capability_id="publish.douyin",
            success=True,
            credits_charged=credits_charged,
            source="publish_api",
        ))
        db.commit()

    resp = {
        "task_id": task.id,
        "status": task.status,
        "result_url": task.result_url,
        "error": task.error,
    }
    if acct.platform == "douyin":
        resp["credits_charged"] = credits_charged
        resp["douyin_billing"] = "unlocked"
    if task.status == "need_login" or (task.meta and task.meta.get("driver_result", {}).get("need_login")):
        resp["need_login"] = True
    logger.info("[PUBLISH-API] response: %s", resp)
    return resp


@router.get("/api/publish/tasks", summary="发布任务列表")
def list_publish_tasks(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(PublishTask)
        .filter(PublishTask.user_id == current_user.id)
        .order_by(PublishTask.created_at.desc())
        .limit(min(limit, 200))
        .all()
    )
    def _task_dict(t):
        meta = t.meta or {}
        driver_result = meta.get("driver_result", {})
        steps = driver_result.get("applied", {}).get("steps", [])
        return {
            "id": t.id,
            "asset_id": t.asset_id,
            "account_id": t.account_id,
            "title": t.title,
            "status": t.status,
            "result_url": t.result_url,
            "error": t.error,
            "platform": meta.get("platform", ""),
            "account_nickname": meta.get("account_nickname", ""),
            "steps": steps,
            "created_at": t.created_at.isoformat() if t.created_at else "",
            "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        }
    return {"tasks": [_task_dict(t) for t in rows]}
