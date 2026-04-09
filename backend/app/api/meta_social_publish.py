"""Meta Social：Instagram / Facebook 自动发布 + 数据同步。

OAuth 流程、账号 CRUD、发布路由、数据同步路由。
回调地址等固定参数由系统自动拼接，用户只需在 .env 配 META_APP_ID / META_APP_SECRET。
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.config import settings, get_effective_public_base_url
from ..db import get_db
from ..models import (
    Asset,
    MetaSocialAccount,
    PublishTask,
    SocialContentSnapshot,
    SocialPublishSchedule,
)
from ..services.meta_graph_api import (
    META_OAUTH_SCOPES,
    GraphAPIError,
    build_httpx_proxy_url,
    exchange_code_for_token,
    exchange_long_lived_token,
    fb_get_page_feed,
    fb_get_page_insights,
    fb_publish_link,
    fb_publish_photo,
    fb_publish_video,
    ig_get_account_insights,
    ig_get_media_insights,
    ig_get_media_list,
    ig_publish_carousel,
    ig_publish_photo,
    ig_publish_reel,
    ig_publish_story,
    ig_publish_video,
    get_user_pages,
)
from .auth import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)

_oauth_states: Dict[str, Dict[str, Any]] = {}
_OAUTH_STATE_TTL = 600


def _public_base() -> str:
    return get_effective_public_base_url().rstrip("/")


def _oauth_redirect_uri() -> str:
    return f"{_public_base()}/api/meta-social/oauth/callback"


def _require_meta_config() -> None:
    if not settings.meta_app_id or not settings.meta_app_secret:
        raise HTTPException(
            status_code=501,
            detail="未配置 META_APP_ID / META_APP_SECRET，请在 .env 中填写 Facebook App 凭据。",
        )


def _prune_states() -> None:
    now = time.time()
    dead = [k for k, v in _oauth_states.items() if v.get("expires", 0) < now]
    for k in dead:
        del _oauth_states[k]


def _proxy_for(acct: MetaSocialAccount) -> Optional[str]:
    return build_httpx_proxy_url(acct.proxy_server, acct.proxy_username, acct.proxy_password)


def _mask_proxy(url: Optional[str]) -> str:
    if not url or not str(url).strip():
        return ""
    s = str(url).strip()
    if len(s) <= 12:
        return "*" * len(s)
    return s[:6] + "..." + s[-6:]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OAuth
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/api/meta-social/oauth/start")
async def meta_oauth_start(
    current_user=Depends(get_current_user),
):
    _require_meta_config()
    _prune_states()

    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {
        "user_id": current_user.id,
        "expires": time.time() + _OAUTH_STATE_TTL,
    }

    fb_url = (
        f"https://www.facebook.com/v21.0/dialog/oauth"
        f"?client_id={settings.meta_app_id}"
        f"&redirect_uri={_oauth_redirect_uri()}"
        f"&scope={META_OAUTH_SCOPES}"
        f"&state={state}"
        f"&response_type=code"
    )
    return {"login_url": fb_url}


@router.get("/api/meta-social/oauth/callback", response_class=HTMLResponse)
async def meta_oauth_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
):
    if error:
        return HTMLResponse(
            f"<h2>授权失败</h2><p>{error}: {error_description}</p>", status_code=400,
        )

    _prune_states()
    st = _oauth_states.pop(state, None)
    if not st:
        return HTMLResponse("<h2>授权失败</h2><p>state 无效或已过期，请重新发起授权。</p>", status_code=400)
    user_id = st["user_id"]

    _require_meta_config()

    try:
        token_data = await exchange_code_for_token(
            settings.meta_app_id, settings.meta_app_secret,
            _oauth_redirect_uri(), code,
        )
        short_token = token_data.get("access_token", "")
        if not short_token:
            return HTMLResponse(f"<h2>授权失败</h2><p>未获取到 access_token: {token_data}</p>", status_code=500)

        ll_data = await exchange_long_lived_token(
            settings.meta_app_id, settings.meta_app_secret, short_token,
        )
        long_token = ll_data.get("access_token", short_token)
        expires_in = ll_data.get("expires_in", 5184000)

        pages = await get_user_pages(long_token)
        if not pages:
            return HTMLResponse("<h2>授权失败</h2><p>未找到授权的 Facebook 主页。请确认您在 Facebook 中管理至少一个主页。</p>", status_code=400)

    except GraphAPIError as e:
        return HTMLResponse(f"<h2>授权失败</h2><p>Graph API 错误: {e.detail}</p>", status_code=500)
    except Exception as e:
        logger.exception("[meta-oauth] callback error")
        return HTMLResponse(f"<h2>授权失败</h2><p>内部错误: {e}</p>", status_code=500)

    saved_count = 0
    for page in pages:
        page_id = page.get("id", "")
        page_name = page.get("name", "")
        page_token = page.get("access_token", "")
        if not page_id or not page_token:
            continue

        ig_biz = page.get("instagram_business_account") or {}
        ig_id = ig_biz.get("id", "")
        ig_username = ig_biz.get("username", "")

        existing = (
            db.query(MetaSocialAccount)
            .filter(MetaSocialAccount.user_id == user_id, MetaSocialAccount.facebook_page_id == page_id)
            .first()
        )
        if existing:
            existing.page_access_token = page_token
            existing.facebook_page_name = page_name
            existing.instagram_business_account_id = ig_id or existing.instagram_business_account_id
            existing.instagram_username = ig_username or existing.instagram_username
            existing.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
            existing.scopes = META_OAUTH_SCOPES
            existing.status = "active"
            existing.updated_at = datetime.utcnow()
        else:
            acct = MetaSocialAccount(
                user_id=user_id,
                label=page_name or f"Page {page_id}",
                facebook_page_id=page_id,
                facebook_page_name=page_name,
                page_access_token=page_token,
                token_expires_at=datetime.utcnow() + timedelta(seconds=expires_in),
                instagram_business_account_id=ig_id or None,
                instagram_username=ig_username or None,
                scopes=META_OAUTH_SCOPES,
                status="active",
            )
            db.add(acct)
        saved_count += 1

    db.commit()

    return HTMLResponse(
        f"<h2>授权成功</h2>"
        f"<p>已连接 {saved_count} 个 Facebook 主页。可以关闭此页面。</p>"
        f"<script>setTimeout(()=>window.close(),3000)</script>"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class MetaAccountOut(BaseModel):
    id: int
    label: str = ""
    facebook_page_id: str
    facebook_page_name: str = ""
    instagram_business_account_id: str = ""
    instagram_username: str = ""
    proxy_server_masked: str = ""
    status: str = "active"
    token_expires_at: Optional[str] = None
    created_at: str = ""

    @classmethod
    def from_orm_row(cls, row: MetaSocialAccount) -> "MetaAccountOut":
        return cls(
            id=row.id,
            label=row.label or "",
            facebook_page_id=row.facebook_page_id,
            facebook_page_name=row.facebook_page_name or "",
            instagram_business_account_id=row.instagram_business_account_id or "",
            instagram_username=row.instagram_username or "",
            proxy_server_masked=_mask_proxy(row.proxy_server),
            status=row.status or "active",
            token_expires_at=row.token_expires_at.isoformat() if row.token_expires_at else None,
            created_at=row.created_at.isoformat() if row.created_at else "",
        )


class MetaAccountPatch(BaseModel):
    label: Optional[str] = None
    proxy_server: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None


@router.get("/api/meta-social/accounts", response_model=List[MetaAccountOut])
async def list_meta_social_accounts(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(MetaSocialAccount)
        .filter(MetaSocialAccount.user_id == current_user.id)
        .order_by(MetaSocialAccount.id)
        .all()
    )
    return [MetaAccountOut.from_orm_row(r) for r in rows]


@router.patch("/api/meta-social/accounts/{account_id}", response_model=MetaAccountOut)
async def patch_meta_social_account(
    account_id: int,
    body: MetaAccountPatch,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = (
        db.query(MetaSocialAccount)
        .filter(MetaSocialAccount.id == account_id, MetaSocialAccount.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")
    if body.label is not None:
        row.label = body.label
    if body.proxy_server is not None:
        row.proxy_server = body.proxy_server or None
    if body.proxy_username is not None:
        row.proxy_username = body.proxy_username or None
    if body.proxy_password is not None:
        row.proxy_password = body.proxy_password or None
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return MetaAccountOut.from_orm_row(row)


@router.delete("/api/meta-social/accounts/{account_id}")
async def delete_meta_social_account(
    account_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = (
        db.query(MetaSocialAccount)
        .filter(MetaSocialAccount.id == account_id, MetaSocialAccount.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 发布
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class MetaPublishBody(BaseModel):
    account_id: int = Field(..., description="MetaSocialAccount.id")
    platform: Literal["instagram", "facebook"] = "instagram"
    content_type: Literal["photo", "video", "carousel", "reel", "story", "link"] = "photo"
    asset_id: Optional[str] = Field(None, description="素材库 asset_id（优先）")
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    caption: str = ""
    message: str = ""
    link: Optional[str] = None
    title: str = ""
    tags: Optional[List[str]] = None
    carousel_items: Optional[List[Dict[str, str]]] = Field(
        None, description='轮播子项 [{"image_url":"..."} or {"video_url":"..."}]',
    )


@router.post("/api/meta-social/publish")
async def meta_social_publish(
    body: MetaPublishBody,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(MetaSocialAccount)
        .filter(MetaSocialAccount.id == body.account_id, MetaSocialAccount.user_id == current_user.id)
        .first()
    )
    if not acct:
        raise HTTPException(status_code=404, detail="Meta 社交账号不存在")
    if acct.status != "active":
        raise HTTPException(status_code=400, detail=f"账号状态为 {acct.status}，无法发布")

    proxy = _proxy_for(acct)
    token = acct.page_access_token

    img_url = body.image_url
    vid_url = body.video_url

    if body.asset_id:
        asset = db.query(Asset).filter(Asset.asset_id == body.asset_id, Asset.user_id == current_user.id).first()
        if not asset:
            raise HTTPException(status_code=404, detail=f"素材 {body.asset_id} 不存在")
        src = (asset.source_url or "").strip()
        if not src:
            raise HTTPException(status_code=400, detail=f"素材 {body.asset_id} 无公网 URL")
        if asset.media_type in ("video", "video/mp4"):
            vid_url = vid_url or src
        else:
            img_url = img_url or src

    caption = body.caption or body.message or ""
    if body.tags:
        tag_str = " ".join(f"#{t.strip().lstrip('#')}" for t in body.tags if t.strip())
        if tag_str:
            caption = f"{caption}\n{tag_str}" if caption else tag_str

    task = PublishTask(
        user_id=current_user.id,
        asset_id=body.asset_id or "",
        account_id=body.account_id,
        title=body.title,
        description=caption,
        tags=",".join(body.tags) if body.tags else None,
        status="pending",
        meta={"platform": body.platform, "content_type": body.content_type, "source": "meta_social"},
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    try:
        post_id = ""

        if body.platform == "instagram":
            ig_id = acct.instagram_business_account_id
            if not ig_id:
                raise HTTPException(status_code=400, detail="该主页未关联 Instagram Business 账号")

            if body.content_type == "photo":
                if not img_url:
                    raise HTTPException(status_code=400, detail="图片发布需提供 image_url 或图片类型的 asset_id")
                post_id = await ig_publish_photo(ig_id, token, img_url, caption, proxy)

            elif body.content_type == "video":
                if not vid_url:
                    raise HTTPException(status_code=400, detail="视频发布需提供 video_url 或视频类型的 asset_id")
                post_id = await ig_publish_video(ig_id, token, vid_url, caption, proxy)

            elif body.content_type == "reel":
                if not vid_url:
                    raise HTTPException(status_code=400, detail="Reel 发布需提供 video_url")
                post_id = await ig_publish_reel(ig_id, token, vid_url, caption, proxy_url=proxy)

            elif body.content_type == "story":
                if not img_url and not vid_url:
                    raise HTTPException(status_code=400, detail="Story 发布需提供 image_url 或 video_url")
                post_id = await ig_publish_story(ig_id, token, image_url=img_url, video_url=vid_url, proxy_url=proxy)

            elif body.content_type == "carousel":
                items = body.carousel_items or []
                if not items:
                    raise HTTPException(status_code=400, detail="轮播发布需提供 carousel_items")
                post_id = await ig_publish_carousel(ig_id, token, items, caption, proxy)

            else:
                raise HTTPException(status_code=400, detail=f"Instagram 不支持 content_type={body.content_type}")

        elif body.platform == "facebook":
            page_id = acct.facebook_page_id

            if body.content_type == "photo":
                if not img_url:
                    raise HTTPException(status_code=400, detail="图片发布需提供 image_url 或图片类型的 asset_id")
                post_id = await fb_publish_photo(page_id, token, img_url, caption, proxy)

            elif body.content_type == "video":
                if not vid_url:
                    raise HTTPException(status_code=400, detail="视频发布需提供 video_url 或视频类型的 asset_id")
                post_id = await fb_publish_video(page_id, token, vid_url, caption, body.title, proxy)

            elif body.content_type == "link":
                post_id = await fb_publish_link(page_id, token, caption, body.link or "", proxy)

            elif body.content_type in ("reel", "story", "carousel"):
                raise HTTPException(status_code=400, detail=f"Facebook 主页不支持 content_type={body.content_type}，请选择 photo/video/link")

            else:
                raise HTTPException(status_code=400, detail=f"不支持的 content_type={body.content_type}")

        task.status = "success"
        task.result_url = post_id
        task.finished_at = datetime.utcnow()
        db.commit()

        logger.info(
            "[meta-publish] ok user=%s acct=%s platform=%s type=%s post_id=%s",
            current_user.id, body.account_id, body.platform, body.content_type, post_id,
        )
        return {"ok": True, "post_id": post_id, "task_id": task.id}

    except HTTPException:
        task.status = "failed"
        task.finished_at = datetime.utcnow()
        db.commit()
        raise
    except GraphAPIError as e:
        task.status = "failed"
        task.error = e.detail[:4000]
        task.finished_at = datetime.utcnow()
        db.commit()
        logger.warning("[meta-publish] Graph API error: %s", e.detail)
        raise HTTPException(status_code=502, detail=f"Graph API 错误: {e.detail}")
    except Exception as e:
        task.status = "failed"
        task.error = str(e)[:4000]
        task.finished_at = datetime.utcnow()
        db.commit()
        logger.exception("[meta-publish] unexpected error")
        raise HTTPException(status_code=500, detail=f"发布失败: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 数据同步
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/api/meta-social/sync")
async def meta_social_sync(
    account_id: Optional[int] = None,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """同步指定账号或全部账号的 IG/FB 数据。"""
    query = db.query(MetaSocialAccount).filter(MetaSocialAccount.user_id == current_user.id)
    if account_id:
        query = query.filter(MetaSocialAccount.id == account_id)
    accounts = query.all()
    if not accounts:
        raise HTTPException(status_code=404, detail="未找到 Meta 社交账号")

    results: List[Dict[str, Any]] = []

    for acct in accounts:
        proxy = _proxy_for(acct)
        token = acct.page_access_token

        # ── Instagram ──
        if acct.instagram_business_account_id:
            ig_id = acct.instagram_business_account_id
            sync_error = None
            items: List[Dict[str, Any]] = []
            account_insights: Dict[str, Any] = {}
            try:
                raw_media = await ig_get_media_list(ig_id, token, proxy_url=proxy)
                for m in raw_media:
                    media_insights = await ig_get_media_insights(m["id"], token, proxy)
                    items.append({
                        "id": m.get("id"),
                        "caption": (m.get("caption") or "")[:500],
                        "media_type": m.get("media_type"),
                        "timestamp": m.get("timestamp"),
                        "permalink": m.get("permalink"),
                        "like_count": m.get("like_count", 0),
                        "comments_count": m.get("comments_count", 0),
                        "insights": media_insights,
                    })
                account_insights = await ig_get_account_insights(ig_id, token, proxy_url=proxy)
            except GraphAPIError as e:
                sync_error = e.detail[:2000]
            except Exception as e:
                sync_error = str(e)[:2000]

            snap = SocialContentSnapshot(
                user_id=current_user.id,
                meta_account_id=acct.id,
                platform="instagram",
                items=items,
                account_insights=account_insights,
                sync_error=sync_error,
            )
            db.add(snap)
            results.append({
                "account_id": acct.id, "platform": "instagram",
                "items_count": len(items), "error": sync_error,
            })

        # ── Facebook ──
        fb_sync_error = None
        fb_items: List[Dict[str, Any]] = []
        fb_insights: Dict[str, Any] = {}
        try:
            raw_feed = await fb_get_page_feed(acct.facebook_page_id, token, proxy_url=proxy)
            for post in raw_feed:
                likes_summary = post.get("likes", {}).get("summary", {})
                comments_summary = post.get("comments", {}).get("summary", {})
                fb_items.append({
                    "id": post.get("id"),
                    "message": (post.get("message") or "")[:500],
                    "type": post.get("type"),
                    "created_time": post.get("created_time"),
                    "permalink_url": post.get("permalink_url"),
                    "likes": likes_summary.get("total_count", 0),
                    "comments": comments_summary.get("total_count", 0),
                    "shares": (post.get("shares") or {}).get("count", 0),
                })
            fb_insights = await fb_get_page_insights(acct.facebook_page_id, token, proxy_url=proxy)
        except GraphAPIError as e:
            fb_sync_error = e.detail[:2000]
        except Exception as e:
            fb_sync_error = str(e)[:2000]

        snap_fb = SocialContentSnapshot(
            user_id=current_user.id,
            meta_account_id=acct.id,
            platform="facebook",
            items=fb_items,
            account_insights=fb_insights,
            sync_error=fb_sync_error,
        )
        db.add(snap_fb)
        results.append({
            "account_id": acct.id, "platform": "facebook",
            "items_count": len(fb_items), "error": fb_sync_error,
        })

    db.commit()
    return {"ok": True, "synced": results}


@router.get("/api/meta-social/data")
async def meta_social_data(
    account_id: Optional[int] = None,
    platform: Optional[str] = None,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读取最近一次同步快照（供 LLM 查询工具使用）。"""
    from sqlalchemy import desc

    query = db.query(MetaSocialAccount).filter(MetaSocialAccount.user_id == current_user.id)
    if account_id:
        query = query.filter(MetaSocialAccount.id == account_id)
    accounts = query.all()

    out: List[Dict[str, Any]] = []
    for acct in accounts:
        for plat in (["instagram", "facebook"] if not platform else [platform]):
            snap = (
                db.query(SocialContentSnapshot)
                .filter(
                    SocialContentSnapshot.user_id == current_user.id,
                    SocialContentSnapshot.meta_account_id == acct.id,
                    SocialContentSnapshot.platform == plat,
                )
                .order_by(desc(SocialContentSnapshot.id))
                .first()
            )
            if not snap:
                continue
            acct_info = {
                "id": acct.id,
                "platform": plat,
                "label": acct.label,
            }
            if plat == "instagram":
                acct_info["username"] = acct.instagram_username or ""
            else:
                acct_info["page_name"] = acct.facebook_page_name or ""

            out.append({
                "account": acct_info,
                "account_metrics": snap.account_insights or {},
                "posts": snap.items or [],
                "fetched_at": snap.fetched_at.isoformat() if snap.fetched_at else None,
                "sync_error": snap.sync_error,
            })

    if not out:
        return {"hint": "暂无同步数据。请先调用 sync_meta_social_data 从 IG/FB 拉取最新数据。", "data": []}
    return {"data": out}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 定时发布 CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class SocialSchedulePut(BaseModel):
    meta_account_id: int
    platform: Literal["instagram", "facebook"] = "instagram"
    content_type: Literal["photo", "video", "reel", "story", "link"] = "photo"
    enabled: bool = False
    interval_minutes: int = Field(60, ge=1, le=10080)
    asset_ids: List[str] = Field(default_factory=list)
    caption: str = ""
    tags: Optional[List[str]] = None


@router.put("/api/meta-social/schedules")
async def put_meta_social_schedule(
    body: SocialSchedulePut,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(MetaSocialAccount)
        .filter(MetaSocialAccount.id == body.meta_account_id, MetaSocialAccount.user_id == current_user.id)
        .first()
    )
    if not acct:
        raise HTTPException(status_code=404, detail="Meta 社交账号不存在")

    sch = (
        db.query(SocialPublishSchedule)
        .filter(
            SocialPublishSchedule.user_id == current_user.id,
            SocialPublishSchedule.meta_account_id == body.meta_account_id,
            SocialPublishSchedule.platform == body.platform,
        )
        .first()
    )
    now = datetime.utcnow()
    if not sch:
        sch = SocialPublishSchedule(
            user_id=current_user.id,
            meta_account_id=body.meta_account_id,
            platform=body.platform,
        )
        db.add(sch)

    sch.content_type = body.content_type
    sch.enabled = body.enabled
    sch.interval_minutes = body.interval_minutes
    sch.asset_ids_json = body.asset_ids
    sch.caption = body.caption
    sch.tags = ",".join(body.tags) if body.tags else None
    if body.enabled and not sch.next_run_at:
        sch.next_run_at = now + timedelta(minutes=body.interval_minutes)

    db.commit()
    return {"ok": True, "schedule_id": sch.id, "next_run_at": sch.next_run_at.isoformat() if sch.next_run_at else None}


@router.get("/api/meta-social/schedules")
async def list_meta_social_schedules(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(SocialPublishSchedule)
        .filter(SocialPublishSchedule.user_id == current_user.id)
        .order_by(SocialPublishSchedule.id)
        .all()
    )
    return [
        {
            "id": r.id,
            "meta_account_id": r.meta_account_id,
            "platform": r.platform,
            "content_type": r.content_type,
            "enabled": r.enabled,
            "interval_minutes": r.interval_minutes,
            "asset_ids": r.asset_ids_json or [],
            "caption": r.caption or "",
            "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None,
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
            "last_run_error": r.last_run_error,
        }
        for r in rows
    ]
