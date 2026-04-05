"""从用户 JWT 解析 sub，并查库取 brand_mark（与 backend SECRET_KEY / HS256 一致）。MCP 需与 Backend 共用库与 SECRET_KEY。"""
from __future__ import annotations

import logging
import os
from typing import Optional

from jose import JWTError, jwt

ALGORITHM = "HS256"

logger = logging.getLogger(__name__)


def _jwt_secret() -> str:
    return (os.environ.get("SECRET_KEY") or os.environ.get("LOBSTER_SECRET_KEY") or "").strip()


def user_id_from_bearer(auth_header: Optional[str]) -> Optional[int]:
    if not auth_header:
        return None
    h = auth_header.strip()
    if not h.lower().startswith("bearer "):
        return None
    token = h[7:].strip()
    if not token:
        return None
    secret = _jwt_secret()
    if not secret:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        uid = payload.get("sub")
        if uid is None:
            return None
        return int(uid)
    except (JWTError, ValueError, TypeError):
        return None


def resolve_brand_mark_for_request(auth_header: Optional[str]) -> Optional[str]:
    """
    MCP 调用速推：仅认 JWT `sub` 在 users 表中的 brand_mark（规范化后有效才返回）。
    查不到用户、库里无品牌、品牌非有效值，或查库异常时，一律返回 None，不使用 JWT payload 兜底。
    """
    uid = user_id_from_bearer(auth_header)
    if uid is None:
        return None
    try:
        from backend.app.api.auth import brand_mark_for_jwt_claim
        from backend.app.db import SessionLocal
        from backend.app.models import User

        db = SessionLocal()
        try:
            u = db.query(User).filter(User.id == uid).first()
            if u is None:
                return None
            bm = brand_mark_for_jwt_claim(getattr(u, "brand_mark", None))
            return bm if bm else None
        finally:
            db.close()
    except Exception as e:
        logger.warning("[jwt_brand] 查库 brand_mark 失败 user_id=%s: %s", uid, e)
        return None
