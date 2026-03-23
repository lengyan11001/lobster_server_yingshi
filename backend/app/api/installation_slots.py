"""在线独立认证：安装身份槽位（每用户最多 3 条，LRU 淘汰最久未用）。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..core.config import settings
from ..models import UserInstallation

INSTALLATION_ID_HEADER = "X-Installation-Id"
MAX_USER_INSTALLATIONS = 3


def installation_slots_enabled() -> bool:
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    return edition == "online" and getattr(settings, "lobster_independent_auth", True)


def parse_installation_id_strict(raw: Optional[str]) -> str:
    """在线版：解析并校验 X-Installation-Id；未启用槽位时返回空串（不校验）。"""
    if not installation_slots_enabled():
        return (raw or "").strip()
    s = (raw or "").strip()
    if not s:
        raise HTTPException(
            status_code=400,
            detail="缺少 X-Installation-Id 请求头，请使用最新客户端。",
        )
    if len(s) < 8 or len(s) > 128:
        raise HTTPException(status_code=400, detail="X-Installation-Id 长度无效。")
    if not re.match(r"^[a-zA-Z0-9\-_]+$", s):
        raise HTTPException(status_code=400, detail="X-Installation-Id 格式无效。")
    return s


def ensure_installation_slot(db: Session, user_id: int, installation_id: str) -> None:
    """登记或刷新 last_seen；满 3 条时删除最久未访问的一条再插入。"""
    if not installation_id:
        return
    now = datetime.utcnow()
    row = (
        db.query(UserInstallation)
        .filter(
            UserInstallation.user_id == user_id,
            UserInstallation.installation_id == installation_id,
        )
        .first()
    )
    if row:
        row.last_seen_at = now
        db.commit()
        return
    n = db.query(UserInstallation).filter(UserInstallation.user_id == user_id).count()
    if n >= MAX_USER_INSTALLATIONS:
        oldest = (
            db.query(UserInstallation)
            .filter(UserInstallation.user_id == user_id)
            .order_by(UserInstallation.last_seen_at.asc())
            .first()
        )
        if oldest is not None:
            db.delete(oldest)
            db.flush()
    db.add(
        UserInstallation(
            user_id=user_id,
            installation_id=installation_id,
            last_seen_at=now,
            created_at=now,
        )
    )
    db.commit()
