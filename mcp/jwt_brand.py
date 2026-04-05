"""从用户 JWT 读取 brand_mark（与 backend SECRET_KEY / HS256 一致）。MCP 进程需配置相同 SECRET_KEY。"""
from __future__ import annotations

import os
from typing import Optional

from jose import JWTError, jwt

ALGORITHM = "HS256"


def _jwt_secret() -> str:
    return (os.environ.get("SECRET_KEY") or os.environ.get("LOBSTER_SECRET_KEY") or "").strip()


def brand_mark_from_bearer(auth_header: Optional[str]) -> Optional[str]:
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
    except JWTError:
        return None
    raw = payload.get("brand_mark")
    if raw is None or raw == "":
        return None
    s = str(raw).strip().lower()
    return s or None
