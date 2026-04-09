"""后台：定时发布 IG / FB 内容（从 SocialPublishSchedule 的 asset_ids_json 队列先进先出）。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Asset, MetaSocialAccount, PublishTask, SocialPublishSchedule
from ..services.meta_graph_api import (
    GraphAPIError,
    build_httpx_proxy_url,
    fb_publish_link,
    fb_publish_photo,
    fb_publish_video,
    ig_publish_carousel,
    ig_publish_photo,
    ig_publish_reel,
    ig_publish_story,
    ig_publish_video,
)

logger = logging.getLogger(__name__)


def _proxy_for(acct: MetaSocialAccount):
    return build_httpx_proxy_url(acct.proxy_server, acct.proxy_username, acct.proxy_password)


async def _run_one(db: Session, sch: SocialPublishSchedule, now: datetime) -> None:
    iv = max(1, int(sch.interval_minutes or 60))
    queue = sch.asset_ids_json if isinstance(sch.asset_ids_json, list) else []

    if not queue:
        sch.last_run_at = now
        sch.last_run_error = "待发布队列为空，已跳过"
        sch.next_run_at = now + timedelta(minutes=iv)
        db.commit()
        return

    acct = db.query(MetaSocialAccount).filter(MetaSocialAccount.id == sch.meta_account_id).first()
    if not acct or acct.status != "active":
        sch.last_run_at = now
        sch.last_run_error = "关联账号不存在或已禁用"
        sch.next_run_at = now + timedelta(minutes=iv)
        db.commit()
        return

    asset_id = queue[0]
    rest = queue[1:]

    asset = db.query(Asset).filter(Asset.asset_id == asset_id).first()
    if not asset:
        sch.last_run_at = now
        sch.last_run_error = f"素材 {asset_id} 不存在"
        sch.asset_ids_json = rest
        sch.next_run_at = now + timedelta(minutes=iv)
        db.commit()
        return

    src_url = (asset.source_url or "").strip()
    if not src_url:
        sch.last_run_at = now
        sch.last_run_error = f"素材 {asset_id} 无公网 URL"
        sch.asset_ids_json = rest
        sch.next_run_at = now + timedelta(minutes=iv)
        db.commit()
        return

    proxy = _proxy_for(acct)
    token = acct.page_access_token
    caption = sch.caption or ""
    ct = sch.content_type or "photo"

    if sch.tags:
        tag_str = " ".join(f"#{t.strip().lstrip('#')}" for t in sch.tags.split(",") if t.strip())
        if tag_str:
            caption = f"{caption}\n{tag_str}" if caption else tag_str

    is_carousel = ct == "carousel" and sch.platform == "instagram"
    carousel_assets = []
    if is_carousel:
        carousel_ids = [asset_id] + rest[:9]
        rest = rest[len(carousel_ids) - 1:]
        for cid in carousel_ids:
            ca = db.query(Asset).filter(Asset.asset_id == cid).first()
            if ca and (ca.source_url or "").strip():
                is_vid = ca.media_type in ("video", "video/mp4")
                carousel_assets.append({"video_url" if is_vid else "image_url": (ca.source_url or "").strip()})

    task = PublishTask(
        user_id=sch.user_id,
        asset_id=asset_id,
        account_id=sch.meta_account_id,
        description=caption,
        status="pending",
        meta={"platform": sch.platform, "content_type": ct, "source": "meta_social_schedule"},
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    try:
        post_id = ""
        is_video = asset.media_type in ("video", "video/mp4")

        if sch.platform == "instagram":
            ig_id = acct.instagram_business_account_id
            if not ig_id:
                raise RuntimeError("该主页未关联 Instagram Business 账号")

            if ct == "carousel":
                if len(carousel_assets) < 2:
                    raise RuntimeError("轮播至少需要 2 张素材")
                post_id = await ig_publish_carousel(ig_id, token, carousel_assets, caption, proxy)
            elif ct == "photo" and not is_video:
                post_id = await ig_publish_photo(ig_id, token, src_url, caption, proxy)
            elif ct == "video" or (ct == "photo" and is_video):
                post_id = await ig_publish_video(ig_id, token, src_url, caption, proxy)
            elif ct == "reel":
                post_id = await ig_publish_reel(ig_id, token, src_url, caption, proxy_url=proxy)
            elif ct == "story":
                kw = {"video_url": src_url} if is_video else {"image_url": src_url}
                post_id = await ig_publish_story(ig_id, token, **kw, proxy_url=proxy)
            else:
                raise RuntimeError(f"不支持的 content_type={ct}")

        elif sch.platform == "facebook":
            page_id = acct.facebook_page_id
            if ct == "photo" and not is_video:
                post_id = await fb_publish_photo(page_id, token, src_url, caption, proxy)
            elif ct == "video" or (ct == "photo" and is_video):
                post_id = await fb_publish_video(page_id, token, src_url, caption, proxy_url=proxy)
            elif ct == "link":
                post_id = await fb_publish_link(page_id, token, caption, src_url, proxy)
            else:
                raise RuntimeError(f"不支持的 content_type={ct}")

        task.status = "success"
        task.result_url = post_id
        task.finished_at = datetime.utcnow()
        sch.asset_ids_json = rest
        sch.last_run_at = datetime.utcnow()
        sch.last_run_error = None
        sch.last_post_id = post_id
        sch.next_run_at = datetime.utcnow() + timedelta(minutes=iv)
        db.commit()
        logger.info("[meta-schedule] ok user=%s asset=%s post=%s", sch.user_id, asset_id, post_id)

    except (GraphAPIError, RuntimeError) as e:
        err_msg = e.detail if isinstance(e, GraphAPIError) else str(e)
        task.status = "failed"
        task.error = err_msg[:4000]
        task.finished_at = datetime.utcnow()
        sch.asset_ids_json = rest
        sch.last_run_at = datetime.utcnow()
        sch.last_run_error = err_msg[:4000]
        sch.next_run_at = datetime.utcnow() + timedelta(minutes=iv)
        db.commit()
        logger.warning("[meta-schedule] failed user=%s asset=%s: %s", sch.user_id, asset_id, err_msg)

    except Exception as e:
        task.status = "failed"
        task.error = str(e)[:4000]
        task.finished_at = datetime.utcnow()
        sch.asset_ids_json = rest
        sch.last_run_at = datetime.utcnow()
        sch.last_run_error = str(e)[:4000]
        sch.next_run_at = datetime.utcnow() + timedelta(minutes=iv)
        db.commit()
        logger.exception("[meta-schedule] error user=%s asset=%s", sch.user_id, asset_id)


async def _tick_once() -> None:
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        rows = (
            db.query(SocialPublishSchedule)
            .filter(
                SocialPublishSchedule.enabled.is_(True),
                SocialPublishSchedule.next_run_at.isnot(None),
                SocialPublishSchedule.next_run_at <= now,
            )
            .all()
        )
        for sch in rows:
            await _run_one(db, sch, now)
    finally:
        db.close()


async def meta_social_schedule_background_loop() -> None:
    await asyncio.sleep(25)
    while True:
        try:
            await _tick_once()
        except Exception:
            logger.exception("[meta-schedule] tick error")
        await asyncio.sleep(50)
