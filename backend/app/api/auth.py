import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import bcrypt
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from ..models import User

router = APIRouter()
logger = logging.getLogger(__name__)
ONLINE_USER_EMAIL = "online@sutui.lobster.local"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())
    id: int
    email: str
    preferred_model: str
    credits: Optional[int] = None


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RegisterBody(BaseModel):
    email: str
    password: str


def _password_to_bcrypt_input(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) <= 72:
        return raw
    return hashlib.sha256(raw).hexdigest().encode("ascii")


def get_password_hash(password: str) -> str:
    data = _password_to_bcrypt_input(password)
    return bcrypt.hashpw(data, bcrypt.gensalt()).decode("ascii")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    data = _password_to_bcrypt_input(plain_password)
    return bcrypt.checkpw(data, hashed_password.encode("ascii"))


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm=ALGORITHM)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无法验证凭证",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        user_id: int = int(payload.get("sub"))
        if user_id is None:
            raise credentials_exception
    except (JWTError, ValueError):
        raise credentials_exception
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception
    return user


@router.post("/login", response_model=Token, summary="登录")
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    access_token = create_access_token(data={"sub": str(user.id)})
    return Token(access_token=access_token)


@router.post("/register", response_model=Token, summary="注册（独立认证时使用）")
def register(body: RegisterBody, db: Session = Depends(get_db)):
    from ..core.config import settings
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    use_independent = getattr(settings, "lobster_independent_auth", True)
    if edition != "online" or not use_independent:
        raise HTTPException(status_code=400, detail="当前版本不支持自主注册")
    email = (body.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="请输入有效邮箱")
    if len(body.password or "") < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=400, detail="该邮箱已注册")
    user = User(
        email=email,
        hashed_password=get_password_hash(body.password),
        credits=0,
        role="user",
        preferred_model="sutui",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    access_token = create_access_token(data={"sub": str(user.id)})
    return Token(access_token=access_token)


@router.get("/me", response_model=UserOut, summary="当前用户信息")
def get_me(current_user: User = Depends(get_current_user)):
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    preferred = "sutui" if edition == "online" else (getattr(current_user, "preferred_model", "openclaw") or "openclaw")
    return UserOut(
        id=current_user.id,
        email=current_user.email,
        preferred_model=preferred,
        credits=getattr(current_user, "credits", None),
    )


# 未配置时使用的默认 xskill 授权页。redirect_uri 放 query 便于读取。
# 实测 https://www.xskill.ai/?redirect_uri=...#/v2/oauth 会打开首页/登录入口，不一定是独立扫码页；
# 若需微信扫码页，请向速推获取正确 OAuth 地址并设置 SUTUI_OAUTH_LOGIN_URL。
_SUTUI_OAUTH_DEFAULT_BASE = "https://www.xskill.ai"
_SUTUI_OAUTH_HASH = "#/v2/oauth"


def _sutui_login_url_with_source(login_url: str) -> str:
    source = (getattr(settings, "sutui_source_id", None) or "").strip()
    if not source:
        return login_url
    sep = "&" if "?" in login_url else "?"
    return f"{login_url}{sep}source={source}"


def _build_sutui_login_url(request: Request, callback_extra: str = "") -> str:
    """构建速推授权页 URL。未配置时用默认页，redirect_uri 放在 query 中便于授权页读取。"""
    url = (getattr(settings, "sutui_oauth_login_url", None) or "").strip()
    if not url:
        base = str(request.base_url).rstrip("/")
        callback = f"{base}/auth/sutui-callback{callback_extra}"
        # query 在前、hash 在后，兼容 SPA 与服务端
        url = f"{_SUTUI_OAUTH_DEFAULT_BASE}/?redirect_uri={quote(callback, safe='')}{_SUTUI_OAUTH_HASH}"
    return _sutui_login_url_with_source(url)


@router.get("/sutui-login-url", summary="在线版：获取速推授权页 URL（embed=1 内嵌用，embed=0 新窗口用）")
def get_sutui_login_url(request: Request, embed: Optional[str] = None):
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition != "online":
        raise HTTPException(status_code=400, detail="此功能仅在线版可用")
    # embed=1 或未传：供 iframe 内嵌（回调带 from=iframe）；embed=0：供新窗口打开（回调直接重定向）
    callback_extra = "" if (embed or "").strip() == "0" else "?from=iframe"
    url = _build_sutui_login_url(request, callback_extra)
    return {"login_url": url}


def _sutui_api_get(path: str) -> dict:
    """GET 速推 API，返回 JSON。"""
    base = (getattr(settings, "sutui_api_base", None) or "https://api.xskill.ai").rstrip("/")
    url = f"{base}{path}"
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    return data


def _sutui_api_post(path: str, body: dict) -> dict:
    """POST 速推 API，返回 JSON。"""
    base = (getattr(settings, "sutui_api_base", None) or "https://api.xskill.ai").rstrip("/")
    url = f"{base}{path}"
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, json=body)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    return data


@router.get("/sutui-qrcode", summary="在线版：获取微信登录二维码（速推 get_qrcode 代理）")
def get_sutui_qrcode():
    """代理 GET api.xskill.ai/api/get_qrcode，返回 url、scene_id 供前端展示二维码并轮询。"""
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition != "online":
        raise HTTPException(status_code=400, detail="此功能仅在线版可用")
    data = _sutui_api_get("/api/get_qrcode")
    url = (data.get("url") or "").strip()
    scene_id = (data.get("scene_id") or "").strip()
    if not url or not scene_id:
        raise HTTPException(status_code=502, detail=data.get("detail") or data.get("msg") or "获取二维码失败")
    return {"url": url, "scene_id": scene_id, "ticket": data.get("ticket")}


class QrcodeStatusBody(BaseModel):
    scene_id: str
    from_user_id: int = 0


@router.post("/sutui-qrcode-status", summary="在线版：轮询微信扫码状态（速推 check_qrcode_status 代理）")
def check_sutui_qrcode_status(body: QrcodeStatusBody):
    """代理 POST api.xskill.ai/api/check_qrcode_status。code 404=等待中，200=data.token 可换 API Key。"""
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition != "online":
        raise HTTPException(status_code=400, detail="此功能仅在线版可用")
    if not (body.scene_id or "").strip():
        raise HTTPException(status_code=400, detail="缺少 scene_id")
    data = _sutui_api_post("/api/check_qrcode_status", {"scene_id": body.scene_id.strip(), "from_user_id": body.from_user_id})
    return data


class LoginWithTokenBody(BaseModel):
    token: str


@router.post("/sutui-login-with-token", summary="在线版：用 JWT 完成登录（换 API Key 并下发本站 token）")
def sutui_login_with_token(body: LoginWithTokenBody, db: Session = Depends(get_db)):
    """扫码拿到 JWT 后调用，与 sutui-callback 逻辑一致：JWT 换 API Key，写库，返回本站 access_token。"""
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition != "online":
        raise HTTPException(status_code=400, detail="此功能仅在线版可用")
    jwt_token = (body.token or "").strip()
    if not jwt_token:
        raise HTTPException(status_code=400, detail="缺少 token")
    if jwt_token.startswith("sk-") and len(jwt_token) >= 10:
        api_key = jwt_token
    else:
        try:
            api_key = _exchange_jwt_for_apikey(jwt_token)
        except ValueError as e:
            logger.warning("sutui_login_with_token exchange failed: %s", e)
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.exception("sutui_login_with_token exchange error")
            raise HTTPException(status_code=502, detail="登录验证失败，请稍后重试")
    user = db.query(User).filter(User.email == ONLINE_USER_EMAIL).first()
    if not user:
        user = User(
            email=ONLINE_USER_EMAIL,
            hashed_password=get_password_hash("online-no-password"),
            credits=99999,
            role="user",
            preferred_model="sutui",
        )
        db.add(user)
        db.flush()
    user.sutui_token = api_key
    db.commit()
    access_token = create_access_token(data={"sub": str(user.id)})
    return Token(access_token=access_token, token_type="bearer")


@router.get("/sutui-login", summary="在线版：跳转微信扫码登录（速推 OAuth）")
def sutui_login(request: Request):
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition != "online":
        raise HTTPException(status_code=400, detail="请使用速推扫码或注册登录")
    url = _build_sutui_login_url(request)
    return RedirectResponse(url=url, status_code=302)


def _exchange_jwt_for_apikey(jwt_token: str) -> str:
    """Call 速推 apikeys/list to get API Key (sk-xxx) from JWT. Raises ValueError on failure."""
    base = (getattr(settings, "sutui_api_base", None) or "https://api.xskill.ai").rstrip("/")
    url = f"{base}/api/v3/apikeys/list"
    payload = {"token": jwt_token, "user_type": 1}
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, json=payload)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if data.get("code") != 200:
        raise ValueError(data.get("detail") or data.get("msg") or "速推验证失败")
    items = (data.get("data") or {}).get("items") or []
    active = next((x for x in items if x.get("status") == "active"), items[0] if items else None)
    if not active or not (key := (active.get("key") or "").strip()):
        raise ValueError("未获取到 API Key")
    return key


@router.get("/sutui-callback", summary="在线版：速推登录回调，携带 token 参数")
def sutui_callback(
    request: Request,
    token: Optional[str] = None,
    from_iframe: Optional[str] = None,
    db: Session = Depends(get_db),
):
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition != "online":
        raise HTTPException(status_code=400, detail="此功能仅在线版可用")
    if not token or not token.strip():
        raise HTTPException(status_code=400, detail="缺少 token 参数")
    jwt_token = token.strip()
    if jwt_token.startswith("sk-") and len(jwt_token) >= 10:
        api_key = jwt_token
    else:
        try:
            api_key = _exchange_jwt_for_apikey(jwt_token)
        except ValueError as e:
            logger.warning("sutui_callback exchange failed: %s", e)
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.exception("sutui_callback exchange error")
            raise HTTPException(status_code=502, detail="登录验证失败，请稍后重试")
    user = db.query(User).filter(User.email == ONLINE_USER_EMAIL).first()
    if not user:
        user = User(
            email=ONLINE_USER_EMAIL,
            hashed_password=get_password_hash("online-no-password"),
            credits=99999,
            role="user",
            preferred_model="sutui",
        )
        db.add(user)
        db.flush()
    user.sutui_token = api_key
    db.commit()
    access_token = create_access_token(data={"sub": str(user.id)})
    # 从 iframe 内回调时返回 HTML，由父页接收 token 并完成登录
    if from_iframe or (request.query_params.get("from") == "iframe"):
        token_js = json.dumps(access_token)
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body><p>登录成功，正在跳转…</p><script>"
            "var t = " + token_js + "; "
            "if (window.parent !== window) { window.parent.postMessage({ type: 'sutui_login_ok', token: t }, '*'); } "
            "else { location.href = '/?token=' + encodeURIComponent(t); }</script></body></html>"
        )
        return HTMLResponse(html)
    return RedirectResponse(url=f"/?token={access_token}", status_code=302)
