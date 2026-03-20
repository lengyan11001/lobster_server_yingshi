"""Asset management: download, store, list, search local media files. 支持 TOS 上传后仅存公网 URL."""
import hmac
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import get_current_user
from ..core.config import settings
from ..db import get_db
from ..models import Asset, User

logger = logging.getLogger(__name__)
router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
ASSETS_DIR = _BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)
TEMP_ASSETS_DIR = _BASE_DIR / "temp_assets"  # 临时文件目录（用于无TOS时的中转）
TEMP_ASSETS_DIR.mkdir(exist_ok=True)
_CUSTOM_CONFIGS_FILE = _BASE_DIR / "custom_configs.json"

# 带签名的临时访问：用于会话里上传的图/视频生成可被速推拉取的 URL
_ASSET_FILE_EXPIRY_SEC = 600  # 10 分钟

# 临时文件跟踪：task_id -> [temp_file_paths]，用于任务完成后清理
_temp_files_by_task: dict[str, list[Path]] = {}


def _get_tos_config() -> Optional[dict]:
    """从 custom_configs.json 读取 TOS_CONFIG，用于上传到 TOS 并得到公网 URL。"""
    if not _CUSTOM_CONFIGS_FILE.exists():
        return None
    try:
        data = json.loads(_CUSTOM_CONFIGS_FILE.read_text(encoding="utf-8"))
        cfg = (data.get("configs") or {}).get("TOS_CONFIG")
        if isinstance(cfg, dict) and cfg.get("access_key") and cfg.get("secret_key"):
            return cfg
    except Exception as e:
        logger.debug("[TOS] 读取 TOS_CONFIG 失败: %s", e)
    return None


def _upload_to_tos(data: bytes, object_key: str, content_type: str) -> Optional[str]:
    """上传字节到 TOS，返回公网可访问 URL；失败返回 None。"""
    cfg = _get_tos_config()
    if not cfg:
        return None
    try:
        import tos
        ak = str(cfg.get("access_key", "")).strip()
        sk = str(cfg.get("secret_key", "")).strip()
        endpoint = str(cfg.get("endpoint", "")).strip()
        region = str(cfg.get("region", "")).strip()
        bucket = str(cfg.get("bucket_name", "")).strip()
        public_domain = str(cfg.get("public_domain", "")).strip().rstrip("/")
        if not all([ak, sk, endpoint, region, bucket, public_domain]):
            logger.warning("[TOS] 配置不完整，跳过上传")
            return None
        client = tos.TosClientV2(ak, sk, endpoint, region)
        client.put_object(bucket, object_key, content=data)
        url = f"{public_domain}/{object_key}"
        logger.info("[TOS] 上传成功 object_key=%s url=%s", object_key, url[:80])
        return url
    except Exception as e:
        logger.exception("[TOS] 上传失败: %s", e)
        return None


def _asset_file_token(asset_id: str, expiry_ts: int) -> str:
    raw = f"{asset_id}:{expiry_ts}"
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_asset_file_url(request: Request, asset_id: str) -> Optional[str]:
    """生成带签名的素材文件访问 URL，供注入到对话消息中（速推可拉取）。保证返回纯 ASCII。
    若速推报 Failed to download：说明其服务器无法访问该 URL，请在 .env 设置 PUBLIC_BASE_URL 为
    速推可访问的地址（公网 IP/域名或内网穿透如 ngrok），勿用 localhost/127.0.0.1/仅局域网 IP。"""
    from ..core.config import get_settings
    expiry_ts = int(time.time()) + _ASSET_FILE_EXPIRY_SEC
    token = _asset_file_token(asset_id, expiry_ts)
    settings = get_settings()
    base = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    if not base:
        try:
            base = str((request.base_url or "").rstrip("/"))
        except Exception:
            base = ""
    if not base:
        base = f"http://127.0.0.1:{getattr(settings, 'port', 8000)}"
    # 强制纯 ASCII，避免速推拉图时出现编码问题
    try:
        base.encode("ascii")
    except UnicodeEncodeError:
        base = f"http://127.0.0.1:{getattr(settings, 'port', 8000)}"
        logger.warning(
            "[素材] base_url 含非 ASCII，已回退为 127.0.0.1。请在 .env 设置 PUBLIC_BASE_URL（如 http://本机局域网IP:8000）以便速推拉取。"
        )
    return f"{base}/api/assets/file/{asset_id}?token={token}&expiry={expiry_ts}"


def get_asset_public_url(
    asset_id: str, user_id: int, request: Request, db: Session
) -> Optional[str]:
    """供速推使用的素材 URL：若 asset 有公网 source_url 则直接返回，否则返回带签名的本地 file URL。
    如果 source_url 是内部地址（如 api.51ins.com），则返回 None，让调用方使用 build_asset_file_url 构建临时 URL，然后由服务器端转存。"""
    row = db.query(Asset).filter(Asset.id == asset_id, Asset.user_id == user_id).first()
    if row and getattr(row, "source_url", None):
        url = (row.source_url or "").strip()
        if url.startswith("http://") or url.startswith("https://"):
            # 检测是否是内部地址（需要转存）
            from urllib.parse import urlparse
            import ipaddress
            try:
                parsed = urlparse(url)
                hostname = (parsed.hostname or "").lower()
                is_internal = (
                    not hostname or
                    hostname in ("localhost", "127.0.0.1", "0.0.0.0") or
                    "api.51ins.com" in hostname or
                    (hostname and ("token=" in url or "?token" in url))
                )
                if not is_internal:
                    try:
                        ip = ipaddress.ip_address(hostname)
                        is_internal = ip.is_private or ip.is_loopback
                    except ValueError:
                        # 不是 IP 地址，检查是否是已知的公开 CDN
                        cdn_keywords = ("cdn.", "oss.", "cos.", "tos.", "s3.", "cloudfront.", "fastly.", "cloudflare.", "img.", "static.", "media.", "assets.", "qiniucdn.", "upyun.", "aliyuncs.", "cdn-video.51sux.com")
                        if any(cdn_keyword in hostname for cdn_keyword in cdn_keywords):
                            is_internal = False
                
                if is_internal:
                    # 内部地址，返回 None，让调用方使用 build_asset_file_url 构建临时 URL，然后由服务器端转存
                    logger.warning("[素材] get_asset_public_url 检测到内部地址，将返回 None 以触发服务器端转存: %s", url[:100])
                    return None
            except Exception as e:
                logger.debug("[素材] get_asset_public_url 检测内部地址失败: %s", e)
            return url
    return build_asset_file_url(request, asset_id)


def _gen_asset_id() -> str:
    return uuid.uuid4().hex[:12]


def _save_bytes(data: bytes, ext: str) -> tuple[str, str, int]:
    """Save raw bytes to local disk, return (asset_id, filename, size)."""
    aid = _gen_asset_id()
    fname = f"{aid}{ext}"
    path = ASSETS_DIR / fname
    path.write_bytes(data)
    return aid, fname, len(data)


def _save_bytes_or_tos(
    data: bytes, ext: str, content_type: str = ""
) -> Tuple[str, str, int, Optional[str]]:
    """有 TOS 时上传到 TOS 并返回公网 URL，不落本地；否则落盘。返回 (asset_id, filename_or_key, size, source_url or None)。"""
    aid = _gen_asset_id()
    object_key = f"assets/{aid}{ext}"
    tos_url = _upload_to_tos(data, object_key, content_type or "application/octet-stream")
    if tos_url:
        return aid, object_key, len(data), tos_url
    fname = f"{aid}{ext}"
    path = ASSETS_DIR / fname
    path.write_bytes(data)
    return aid, fname, len(data), None


# ── Download from URL ─────────────────────────────────────────────

class SaveAssetReq(BaseModel):
    url: str
    media_type: str = "image"
    name: Optional[str] = None
    tags: Optional[str] = None
    prompt: Optional[str] = None
    model: Optional[str] = None


@router.post("/api/assets/save-url", summary="从 URL 保存素材")
async def save_asset_from_url(
    body: SaveAssetReq,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as c:
            resp = await c.get(body.url)
            resp.raise_for_status()
            data = resp.content
    except Exception as e:
        raise HTTPException(400, detail=f"下载失败: {e}")

    url_path = body.url.split("?")[0].split("#")[0]
    url_ext = Path(url_path).suffix.lower() if "." in url_path.split("/")[-1] else ""
    ct = resp.headers.get("content-type", "")
    ext = url_ext or ".png"
    if not url_ext:
        if "jpeg" in ct or "jpg" in ct:
            ext = ".jpg"
        elif "webp" in ct:
            ext = ".webp"
        elif "gif" in ct:
            ext = ".gif"
        elif "mp4" in ct or "video/mp4" in ct:
            ext = ".mp4"
        elif "webm" in ct:
            ext = ".webm"
        elif "mov" in ct or "quicktime" in ct:
            ext = ".mov"

    if body.media_type == "video" and ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        ext = ".mp4"
    elif body.media_type == "image" and ext in (".mp4", ".webm", ".mov", ".avi"):
        ext = ".png"

    ct = resp.headers.get("content-type", "") or ""
    aid, fname_or_key, fsize, tos_public_url = _save_bytes_or_tos(data, ext, ct)
    source_url = tos_public_url if tos_public_url else body.url
    asset = Asset(
        asset_id=aid,
        user_id=current_user.id,
        filename=fname_or_key,
        media_type=body.media_type,
        file_size=fsize,
        source_url=source_url,
        prompt=body.prompt,
        model=body.model,
        tags=body.tags,
    )
    db.add(asset)
    db.commit()
    logger.info("[素材] save-url 完成 url=%s asset_id=%s size=%s media_type=%s tos=%s", body.url[:80] + ("..." if len(body.url) > 80 else ""), aid, fsize, body.media_type, bool(tos_public_url))
    return {
        "asset_id": aid,
        "filename": fname_or_key,
        "media_type": body.media_type,
        "file_size": fsize,
    }


# ── Upload file ───────────────────────────────────────────────────

@router.post("/api/assets/upload", summary="上传素材文件")
async def upload_asset(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, detail="文件为空")

    name = file.filename or "upload"
    ext = Path(name).suffix or ".bin"
    mtype = "image"
    if ext.lower() in (".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv", ".wmv"):
        mtype = "video"
    elif ext.lower() in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"):
        mtype = "audio"

    content_type = getattr(file, "content_type", "") or ""
    aid, fname_or_key, fsize, tos_public_url = _save_bytes_or_tos(data, ext, content_type)
    asset = Asset(
        asset_id=aid,
        user_id=current_user.id,
        filename=fname_or_key,
        media_type=mtype,
        file_size=fsize,
        source_url=tos_public_url,
    )
    db.add(asset)
    db.commit()
    return {"asset_id": aid, "filename": fname_or_key, "media_type": mtype, "file_size": fsize}


# ── Temporary file upload (for clients without TOS) ───────────────

class TempUploadResponse(BaseModel):
    temp_id: str
    public_url: str


@router.post("/api/assets/upload-temp", summary="上传临时文件（无TOS时使用）")
async def upload_temp_file(
    file: UploadFile = File(...),
    request: Request = None,
    current_user: User = Depends(get_current_user),
):
    """接收客户端上传的临时文件，返回可访问的URL。这些文件将在视频生成任务完成后自动删除。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, detail="文件为空")
    
    # 生成临时文件ID
    temp_id = f"temp_{uuid.uuid4().hex[:16]}"
    name = file.filename or "upload"
    ext = Path(name).suffix or ".bin"
    temp_filename = f"{temp_id}{ext}"
    temp_path = TEMP_ASSETS_DIR / temp_filename
    
    # 保存临时文件
    temp_path.write_bytes(data)
    logger.info("[临时文件] 上传成功 temp_id=%s filename=%s size=%d", temp_id, temp_filename, len(data))
    
    # 生成可访问的URL
    from ..core.config import get_settings
    settings = get_settings()
    base = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    if not base and request:
        try:
            base = str((request.base_url or "").rstrip("/"))
        except Exception:
            pass
    if not base:
        base = "http://47.120.39.220:8000"  # 服务器公网地址（默认值）
    expiry_ts = int(time.time()) + _ASSET_FILE_EXPIRY_SEC
    public_url = f"{base}/api/assets/temp/{temp_id}?token={_asset_file_token(temp_id, expiry_ts)}&expiry={expiry_ts}"
    
    return TempUploadResponse(temp_id=temp_id, public_url=public_url)


@router.get("/api/assets/temp/{temp_id}", summary="访问临时文件")
async def get_temp_file(
    temp_id: str,
    token: str = Query(...),
    expiry: int = Query(...),
):
    """提供临时文件的访问接口，带签名验证。"""
    # 验证token
    expected_token = _asset_file_token(temp_id, expiry)
    if not hmac.compare_digest(token, expected_token):
        raise HTTPException(403, detail="无效的token")
    
    # 检查过期
    if int(time.time()) > expiry:
        raise HTTPException(403, detail="URL已过期")
    
    # 查找临时文件
    temp_files = list(TEMP_ASSETS_DIR.glob(f"{temp_id}.*"))
    if not temp_files:
        raise HTTPException(404, detail="临时文件不存在或已删除")
    
    temp_path = temp_files[0]
    if not temp_path.exists():
        raise HTTPException(404, detail="临时文件不存在")
    
    return FileResponse(
        temp_path,
        media_type="application/octet-stream",
        filename=temp_path.name,
    )


def register_temp_file_for_task(task_id: str, temp_id: str):
    """注册临时文件与任务ID的关联，用于任务完成后清理。"""
    if task_id not in _temp_files_by_task:
        _temp_files_by_task[task_id] = []
    
    # 查找临时文件路径
    temp_files = list(TEMP_ASSETS_DIR.glob(f"{temp_id}.*"))
    if temp_files:
        _temp_files_by_task[task_id].append(temp_files[0])
        logger.info("[临时文件] 注册 task_id=%s temp_id=%s path=%s", task_id, temp_id, temp_files[0])


def cleanup_temp_files_for_task(task_id: str):
    """清理指定任务关联的临时文件。"""
    if task_id not in _temp_files_by_task:
        return
    
    deleted_count = 0
    for temp_path in _temp_files_by_task[task_id]:
        try:
            if temp_path.exists():
                temp_path.unlink()
                deleted_count += 1
                logger.info("[临时文件] 已删除 task_id=%s path=%s", task_id, temp_path)
        except Exception as e:
            logger.warning("[临时文件] 删除失败 task_id=%s path=%s error=%s", task_id, temp_path, e)
    
    del _temp_files_by_task[task_id]
    if deleted_count > 0:
        logger.info("[临时文件] 任务完成清理 task_id=%s 删除文件数=%d", task_id, deleted_count)


# ── List / search ─────────────────────────────────────────────────

@router.get("/api/assets", summary="列出本地素材")
def list_assets(
    media_type: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Asset).filter(Asset.user_id == current_user.id)
    if media_type:
        query = query.filter(Asset.media_type == media_type)
    if q:
        pat = f"%{q}%"
        query = query.filter(
            (Asset.tags.ilike(pat))
            | (Asset.prompt.ilike(pat))
            | (Asset.filename.ilike(pat))
        )
    total = query.count()
    rows = query.order_by(Asset.created_at.desc()).offset(offset).limit(min(limit, 200)).all()
    return {
        "total": total,
        "assets": [
            {
                "asset_id": r.asset_id,
                "filename": r.filename,
                "media_type": r.media_type,
                "file_size": r.file_size,
                "source_url": r.source_url,
                "prompt": r.prompt,
                "model": r.model,
                "tags": r.tags,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ],
    }


def _asset_local_path(asset: Asset) -> Optional[Path]:
    """有本地文件时返回路径，仅 TOS（无本地）时返回 None。"""
    fn = asset.filename or ""
    if "/" in fn:
        return None
    p = ASSETS_DIR / fn
    return p if p.exists() else None


# ── Get single + serve file ──────────────────────────────────────

@router.get("/api/assets/{asset_id}/content", summary="素材文件内容（需登录，用于前端预览）")
def get_asset_content(
    asset_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    a = db.query(Asset).filter(Asset.asset_id == asset_id, Asset.user_id == current_user.id).first()
    if not a:
        raise HTTPException(404, detail="素材不存在")
    local_path = _asset_local_path(a)
    if local_path is None and getattr(a, "source_url", None):
        url = (a.source_url or "").strip()
        if url.startswith("http://") or url.startswith("https://"):
            return RedirectResponse(url=url)
    if local_path is None:
        raise HTTPException(404, detail="文件不存在")
    mt_map = {"image": "image/jpeg", "video": "video/mp4", "audio": "audio/mpeg"}
    ct = mt_map.get((a.media_type or "").lower(), "application/octet-stream")
    return FileResponse(local_path, media_type=ct, filename=a.filename)


@router.get("/api/assets/file/{asset_id}", summary="素材文件（带签名公开访问，供速推等拉取）")
def serve_asset_file(
    asset_id: str,
    token: str = Query(..., description="签名 token"),
    expiry: int = Query(..., description="过期时间戳"),
    db: Session = Depends(get_db),
):
    """不校验登录，仅校验 token 与 expiry；用于会话附图/视频时生成可被上游拉取的 URL。仅 TOS 时重定向到公网 URL。"""
    now = int(time.time())
    if expiry < now:
        raise HTTPException(403, detail="链接已过期")
    expected = _asset_file_token(asset_id, expiry)
    if not hmac.compare_digest(expected, token):
        raise HTTPException(403, detail="无效链接")
    a = db.query(Asset).filter(Asset.asset_id == asset_id).first()
    if not a:
        raise HTTPException(404, detail="素材不存在")
    local_path = _asset_local_path(a)
    if local_path is None and getattr(a, "source_url", None):
        url = (a.source_url or "").strip()
        if url.startswith("http://") or url.startswith("https://"):
            return RedirectResponse(url=url)
    if local_path is None:
        raise HTTPException(404, detail="文件不存在")
    media_type = a.media_type or "application/octet-stream"
    mt_map = {"image": "image/jpeg", "video": "video/mp4", "audio": "audio/mpeg"}
    ct = mt_map.get(media_type, "application/octet-stream")
    return FileResponse(local_path, media_type=ct, filename=a.filename)


@router.get("/api/assets/{asset_id}", summary="获取素材详情")
def get_asset(
    asset_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    a = db.query(Asset).filter(Asset.asset_id == asset_id, Asset.user_id == current_user.id).first()
    if not a:
        raise HTTPException(404, detail="素材不存在")
    local_path = _asset_local_path(a)
    out = {
        "asset_id": a.asset_id,
        "filename": a.filename,
        "media_type": a.media_type,
        "file_size": a.file_size,
        "source_url": a.source_url,
        "prompt": a.prompt,
        "tags": a.tags,
        "created_at": a.created_at.isoformat() if a.created_at else "",
    }
    if local_path is not None:
        out["local_path"] = str(local_path)
    else:
        out["local_path"] = None
    return out


@router.delete("/api/assets/{asset_id}", summary="删除素材")
def delete_asset(
    asset_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    a = db.query(Asset).filter(Asset.asset_id == asset_id, Asset.user_id == current_user.id).first()
    if not a:
        raise HTTPException(404, detail="素材不存在")
    local_path = _asset_local_path(a)
    if local_path is not None and local_path.exists():
        local_path.unlink()
    db.delete(a)
    db.commit()
    return {"ok": True}
