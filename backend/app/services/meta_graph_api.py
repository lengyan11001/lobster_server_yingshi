"""Meta Graph API 封装：Instagram Content Publishing + Facebook Pages Publishing + Insights。

所有 HTTP 请求走 httpx，支持每账号独立代理（防风控）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

import httpx

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

META_OAUTH_SCOPES = (
    "pages_manage_posts,"
    "pages_read_engagement,"
    "pages_show_list,"
    "instagram_basic,"
    "instagram_content_publish,"
    "instagram_manage_insights,"
    "publish_video"
)


def build_httpx_proxy_url(
    proxy_server: Optional[str],
    proxy_username: Optional[str] = None,
    proxy_password: Optional[str] = None,
) -> Optional[str]:
    raw = (proxy_server or "").strip()
    if not raw:
        return None
    u = urlparse(raw)
    if u.scheme not in ("http", "https"):
        raise ValueError("代理地址须以 http:// 或 https:// 开头，例如 http://1.2.3.4:8080")
    host = u.hostname
    if not host:
        raise ValueError("代理 URL 中缺少主机名")
    port = u.port if u.port is not None else (443 if u.scheme == "https" else 8080)
    user = (proxy_username or "").strip() or (unquote(u.username) if u.username else "")
    pw = (proxy_password or "").strip() or (unquote(u.password) if u.password else "")
    if user or pw:
        netloc = f"{quote(user, safe='')}:{quote(pw, safe='')}@{host}:{port}"
    else:
        netloc = f"{host}:{port}"
    return f"{u.scheme}://{netloc}"


def _client(proxy_url: Optional[str] = None, timeout: float = 60.0) -> httpx.AsyncClient:
    kwargs: Dict[str, Any] = {"timeout": timeout}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return httpx.AsyncClient(**kwargs)


class GraphAPIError(Exception):
    def __init__(self, status_code: int, detail: str, raw: Any = None):
        self.status_code = status_code
        self.detail = detail
        self.raw = raw
        super().__init__(detail)


async def _graph_request(
    method: str,
    path: str,
    token: str,
    proxy_url: Optional[str] = None,
    timeout: float = 60.0,
    **kwargs: Any,
) -> Dict[str, Any]:
    url = f"{GRAPH_BASE}/{path.lstrip('/')}" if not path.startswith("http") else path
    params = kwargs.pop("params", {})
    params["access_token"] = token
    async with _client(proxy_url, timeout) as client:
        r = await client.request(method, url, params=params, **kwargs)
    if r.status_code >= 400:
        try:
            body = r.json()
            msg = body.get("error", {}).get("message", r.text[:500])
        except Exception:
            msg = r.text[:500]
        raise GraphAPIError(r.status_code, msg, r.text[:2000])
    return r.json()


# ━━━ OAuth 工具函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def exchange_code_for_token(
    app_id: str, app_secret: str, redirect_uri: str, code: str,
    proxy_url: Optional[str] = None,
) -> Dict[str, Any]:
    """code → 短期 User Access Token。"""
    async with _client(proxy_url) as client:
        r = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
    if r.status_code >= 400:
        raise GraphAPIError(r.status_code, f"token 交换失败: {r.text[:500]}")
    return r.json()


async def exchange_long_lived_token(
    app_id: str, app_secret: str, short_token: str,
    proxy_url: Optional[str] = None,
) -> Dict[str, Any]:
    """短期 User Token → 长期 User Token（~60 天）。"""
    async with _client(proxy_url) as client:
        r = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token,
            },
        )
    if r.status_code >= 400:
        raise GraphAPIError(r.status_code, f"长期 token 交换失败: {r.text[:500]}")
    return r.json()


async def get_user_pages(
    user_token: str, proxy_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """用长期 User Token 列出授权的 Pages + 每页的 Page Access Token。"""
    data = await _graph_request(
        "GET", "me/accounts",
        user_token, proxy_url,
        params={"fields": "id,name,access_token,instagram_business_account{id,username}"},
    )
    return data.get("data", [])


async def refresh_long_lived_page_token(
    page_token: str, proxy_url: Optional[str] = None,
) -> str:
    """刷新 Page Access Token（需在过期前调用，返回新的长期 token）。
    注：Page Access Token 从长期 User Token 获取时本身不过期；
    此函数用于从普通 Page Token 延长，按 Graph API /oauth/access_token?grant_type=fb_exchange_token 模式。
    如果原始 Page Token 已来自长期 User Token 的 me/accounts，它就是永久的，不需要刷新。
    """
    data = await _graph_request("GET", "me", page_token, proxy_url, params={"fields": "id"})
    return page_token


# ━━━ Instagram Publishing ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def ig_create_media_container(
    ig_user_id: str,
    token: str,
    *,
    media_type: Optional[str] = None,
    image_url: Optional[str] = None,
    video_url: Optional[str] = None,
    caption: str = "",
    share_to_feed: bool = True,
    is_carousel_item: bool = False,
    children: Optional[List[str]] = None,
    proxy_url: Optional[str] = None,
) -> str:
    """创建 IG Media Container，返回 container_id (creation_id)。"""
    data: Dict[str, Any] = {}
    if caption:
        data["caption"] = caption

    if media_type and media_type.upper() == "CAROUSEL":
        data["media_type"] = "CAROUSEL"
        if children:
            data["children"] = ",".join(children)
    elif media_type and media_type.upper() == "REELS":
        data["media_type"] = "REELS"
        if video_url:
            data["video_url"] = video_url
        data["share_to_feed"] = "true" if share_to_feed else "false"
    elif media_type and media_type.upper() == "STORIES":
        data["media_type"] = "STORIES"
        if video_url:
            data["video_url"] = video_url
        elif image_url:
            data["image_url"] = image_url
    elif video_url:
        data["media_type"] = "VIDEO"
        data["video_url"] = video_url
    elif image_url:
        data["image_url"] = image_url
    else:
        raise ValueError("必须提供 image_url 或 video_url")

    if is_carousel_item:
        data["is_carousel_item"] = "true"

    result = await _graph_request(
        "POST", f"{ig_user_id}/media", token, proxy_url,
        data=data, timeout=120.0,
    )
    cid = result.get("id")
    if not cid:
        raise GraphAPIError(500, f"创建 IG container 未返回 id: {result}")
    return str(cid)


async def ig_poll_container_status(
    container_id: str, token: str, proxy_url: Optional[str] = None,
) -> str:
    """轮询 container 状态，返回 status_code: FINISHED / IN_PROGRESS / ERROR。"""
    data = await _graph_request(
        "GET", container_id, token, proxy_url,
        params={"fields": "status_code,status"},
    )
    return data.get("status_code", "UNKNOWN")


async def ig_publish_container(
    ig_user_id: str, container_id: str, token: str,
    proxy_url: Optional[str] = None,
) -> str:
    """发布 container，返回 media_id。"""
    result = await _graph_request(
        "POST", f"{ig_user_id}/media_publish", token, proxy_url,
        data={"creation_id": container_id},
    )
    mid = result.get("id")
    if not mid:
        raise GraphAPIError(500, f"IG 发布未返回 id: {result}")
    return str(mid)


async def ig_publish_photo(
    ig_user_id: str, token: str, image_url: str, caption: str = "",
    proxy_url: Optional[str] = None,
) -> str:
    cid = await ig_create_media_container(
        ig_user_id, token, image_url=image_url, caption=caption, proxy_url=proxy_url,
    )
    return await ig_publish_container(ig_user_id, cid, token, proxy_url)


async def ig_publish_video(
    ig_user_id: str, token: str, video_url: str, caption: str = "",
    proxy_url: Optional[str] = None, poll_interval: float = 5.0, max_polls: int = 120,
) -> str:
    import asyncio
    cid = await ig_create_media_container(
        ig_user_id, token, video_url=video_url, caption=caption, proxy_url=proxy_url,
    )
    for _ in range(max_polls):
        st = await ig_poll_container_status(cid, token, proxy_url)
        if st == "FINISHED":
            break
        if st == "ERROR":
            raise GraphAPIError(500, f"IG 视频处理失败 container={cid}")
        await asyncio.sleep(poll_interval)
    else:
        raise GraphAPIError(504, f"IG 视频处理超时 container={cid}")
    return await ig_publish_container(ig_user_id, cid, token, proxy_url)


async def ig_publish_reel(
    ig_user_id: str, token: str, video_url: str, caption: str = "",
    share_to_feed: bool = True, proxy_url: Optional[str] = None,
    poll_interval: float = 5.0, max_polls: int = 120,
) -> str:
    import asyncio
    cid = await ig_create_media_container(
        ig_user_id, token, media_type="REELS", video_url=video_url,
        caption=caption, share_to_feed=share_to_feed, proxy_url=proxy_url,
    )
    for _ in range(max_polls):
        st = await ig_poll_container_status(cid, token, proxy_url)
        if st == "FINISHED":
            break
        if st == "ERROR":
            raise GraphAPIError(500, f"IG Reel 处理失败 container={cid}")
        await asyncio.sleep(poll_interval)
    else:
        raise GraphAPIError(504, f"IG Reel 处理超时 container={cid}")
    return await ig_publish_container(ig_user_id, cid, token, proxy_url)


async def ig_publish_story(
    ig_user_id: str, token: str, *,
    image_url: Optional[str] = None, video_url: Optional[str] = None,
    proxy_url: Optional[str] = None,
    poll_interval: float = 5.0, max_polls: int = 120,
) -> str:
    import asyncio
    cid = await ig_create_media_container(
        ig_user_id, token, media_type="STORIES",
        image_url=image_url, video_url=video_url, proxy_url=proxy_url,
    )
    if video_url:
        for _ in range(max_polls):
            st = await ig_poll_container_status(cid, token, proxy_url)
            if st == "FINISHED":
                break
            if st == "ERROR":
                raise GraphAPIError(500, f"IG Story 处理失败 container={cid}")
            await asyncio.sleep(poll_interval)
        else:
            raise GraphAPIError(504, f"IG Story 处理超时 container={cid}")
    return await ig_publish_container(ig_user_id, cid, token, proxy_url)


async def ig_publish_carousel(
    ig_user_id: str, token: str, items: List[Dict[str, str]], caption: str = "",
    proxy_url: Optional[str] = None,
    poll_interval: float = 5.0, max_polls: int = 120,
) -> str:
    """items: [{"image_url": "..."} or {"video_url": "..."}]"""
    import asyncio
    child_ids: List[str] = []
    for item in items:
        img = item.get("image_url")
        vid = item.get("video_url")
        cid = await ig_create_media_container(
            ig_user_id, token, image_url=img, video_url=vid,
            is_carousel_item=True, proxy_url=proxy_url,
        )
        if vid:
            for _ in range(max_polls):
                st = await ig_poll_container_status(cid, token, proxy_url)
                if st == "FINISHED":
                    break
                if st == "ERROR":
                    raise GraphAPIError(500, f"轮播子项处理失败 container={cid}")
                await asyncio.sleep(poll_interval)
            else:
                raise GraphAPIError(504, f"轮播子项处理超时 container={cid}")
        child_ids.append(cid)

    carousel_cid = await ig_create_media_container(
        ig_user_id, token, media_type="CAROUSEL", caption=caption,
        children=child_ids, proxy_url=proxy_url,
    )
    return await ig_publish_container(ig_user_id, carousel_cid, token, proxy_url)


# ━━━ Facebook Page Publishing ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def fb_publish_photo(
    page_id: str, token: str, image_url: str, message: str = "",
    proxy_url: Optional[str] = None,
) -> str:
    result = await _graph_request(
        "POST", f"{page_id}/photos", token, proxy_url,
        data={"url": image_url, "message": message},
    )
    return str(result.get("id", ""))


async def fb_publish_video(
    page_id: str, token: str, video_url: str, description: str = "",
    title: str = "", proxy_url: Optional[str] = None,
) -> str:
    result = await _graph_request(
        "POST", f"{page_id}/videos", token, proxy_url,
        data={"file_url": video_url, "description": description, "title": title},
        timeout=300.0,
    )
    return str(result.get("id", ""))


async def fb_publish_link(
    page_id: str, token: str, message: str = "", link: str = "",
    proxy_url: Optional[str] = None,
) -> str:
    data: Dict[str, str] = {}
    if message:
        data["message"] = message
    if link:
        data["link"] = link
    result = await _graph_request(
        "POST", f"{page_id}/feed", token, proxy_url, data=data,
    )
    return str(result.get("id", ""))


# ━━━ Insights ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def ig_get_media_list(
    ig_user_id: str, token: str, limit: int = 50,
    proxy_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    data = await _graph_request(
        "GET", f"{ig_user_id}/media", token, proxy_url,
        params={
            "fields": "id,caption,media_type,media_url,thumbnail_url,timestamp,permalink,like_count,comments_count",
            "limit": str(limit),
        },
    )
    return data.get("data", [])


async def ig_get_media_insights(
    media_id: str, token: str, proxy_url: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        data = await _graph_request(
            "GET", f"{media_id}/insights", token, proxy_url,
            params={"metric": "engagement,impressions,reach,saved"},
        )
        metrics: Dict[str, Any] = {}
        for item in data.get("data", []):
            metrics[item["name"]] = item.get("values", [{}])[0].get("value", 0)
        return metrics
    except GraphAPIError:
        return {}


async def ig_get_account_insights(
    ig_user_id: str, token: str, period: str = "day",
    proxy_url: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        data = await _graph_request(
            "GET", f"{ig_user_id}/insights", token, proxy_url,
            params={
                "metric": "impressions,reach,profile_views,follower_count",
                "period": period,
            },
        )
        metrics: Dict[str, Any] = {}
        for item in data.get("data", []):
            values = item.get("values", [])
            metrics[item["name"]] = values[-1].get("value", 0) if values else 0
        return metrics
    except GraphAPIError as e:
        logger.warning("[meta-insights] IG 账号指标获取失败: %s", e.detail)
        return {}


async def fb_get_page_feed(
    page_id: str, token: str, limit: int = 50,
    proxy_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    data = await _graph_request(
        "GET", f"{page_id}/feed", token, proxy_url,
        params={
            "fields": "id,message,created_time,permalink_url,type,shares,likes.summary(true),comments.summary(true)",
            "limit": str(limit),
        },
    )
    return data.get("data", [])


async def fb_get_page_insights(
    page_id: str, token: str, period: str = "days_28",
    proxy_url: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        data = await _graph_request(
            "GET", f"{page_id}/insights", token, proxy_url,
            params={
                "metric": "page_impressions,page_engaged_users,page_post_engagements,page_fans",
                "period": period,
            },
        )
        metrics: Dict[str, Any] = {}
        for item in data.get("data", []):
            values = item.get("values", [])
            metrics[item["name"]] = values[-1].get("value", 0) if values else 0
        return metrics
    except GraphAPIError as e:
        logger.warning("[meta-insights] FB 主页指标获取失败: %s", e.detail)
        return {}
