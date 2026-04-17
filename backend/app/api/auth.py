import base64
import hashlib
import json
import logging
import random
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional
from urllib.parse import quote

import bcrypt
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..core.config import settings, get_effective_public_base_url
from ..captcha_util import create_captcha, verify_captcha
from ..db import get_db
from ..models import SkillUnlock, User
from ..services.credits_amount import credits_json_float
from ..services.sms_ihuyi import send_verify_code_sms
from .installation_slots import (
    INSTALLATION_ID_HEADER,
    apply_installation_signup_bonus_for_new_user,
    ensure_installation_slot,
    installation_slots_enabled,
    optional_installation_id_from_request,
    parse_installation_id_strict,
)

router = APIRouter()
logger = logging.getLogger(__name__)
ONLINE_USER_EMAIL = "online@sutui.lobster.local"

_SMS_LOCK = threading.Lock()
_SMS_CODE_STORE: dict[str, tuple[str, float]] = {}
_SMS_SEND_AT: dict[str, float] = {}
_SMS_SEND_HOUR_COUNT: dict[str, tuple[float, int]] = {}
SMS_CODE_TTL_SEC = 600
SMS_SEND_COOLDOWN_SEC = 60
SMS_MAX_PER_HOUR = 10
PHONE_EMAIL_SUFFIX = "@sms.lobster.local"
_CN_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
# 新注册用户满额新人分（在线独立认证下按 installation_id 仅首注发放，同机再注册为 0），最多 4 位小数
REGISTER_INITIAL_CREDITS = Decimal("1000.0000")
DEFAULT_ONLINE_USER_CREDITS = Decimal("99999.0000")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())
    id: int
    email: str
    preferred_model: str
    credits: Optional[float] = None
    brand_mark: Optional[str] = None
    wecom_userid: Optional[str] = None
    is_agent: bool = False


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


_BRAND_MARK_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


def _normalize_brand_mark(raw: Optional[str]) -> Optional[str]:
    """注册请求中的品牌标记；空则不入库。须为小写 slug（与 brands.json 的 marks 键一致）。"""
    if raw is None:
        return None
    s = (raw or "").strip().lower()
    if not s:
        return None
    if not _BRAND_MARK_RE.match(s):
        raise HTTPException(status_code=400, detail="品牌标记格式无效")
    return s


def brand_mark_for_jwt_claim(raw: Optional[str]) -> Optional[str]:
    """写入 JWT 的品牌字段：非法或空则省略（不在此处抛错）。"""
    if raw is None:
        return None
    s = (raw or "").strip().lower()
    if not s or not _BRAND_MARK_RE.match(s):
        return None
    return s


def access_token_claims(user: User) -> dict:
    claims: dict = {"sub": str(user.id)}
    bm = brand_mark_for_jwt_claim(getattr(user, "brand_mark", None))
    if bm:
        claims["brand_mark"] = bm
    return claims


class RegisterBody(BaseModel):
    account: str  # 字母开头，2～64 位，仅允许字母数字._-
    password: str
    captcha_id: str = ""
    captcha_answer: str = ""
    brand_mark: Optional[str] = None


class SmsSendBody(BaseModel):
    phone: str
    captcha_id: str = ""
    captcha_answer: str = ""


class RegisterPhoneBody(BaseModel):
    phone: str
    code: str
    password: str
    brand_mark: Optional[str] = None
    parent_account: Optional[str] = None


def _normalize_cn_mobile(raw: str) -> str:
    d = re.sub(r"\D", "", (raw or "").strip())
    if not _CN_MOBILE_RE.match(d):
        raise HTTPException(status_code=400, detail="手机号格式无效")
    return d


def _phone_account_email(mobile: str) -> str:
    return f"{mobile}{PHONE_EMAIL_SUFFIX}"


def _purge_sms_stale_locked(now_m: float) -> None:
    for k in [x for x, v in _SMS_CODE_STORE.items() if v[1] <= now_m]:
        del _SMS_CODE_STORE[k]


def _login_account_key(username: str) -> str:
    """登录框：含 @ 按邮箱；否则 11 位手机号映射为手机账号邮箱。"""
    u_raw = (username or "").strip()
    if not u_raw:
        return ""
    if "@" in u_raw:
        return u_raw.lower()
    digits_only = re.sub(r"\D", "", u_raw)
    if _CN_MOBILE_RE.match(digits_only):
        return _phone_account_email(digits_only)
    return u_raw.lower()


# 账号校验规则：字母开头，2～64 位，仅允许 [a-zA-Z0-9._-]，查重与存库统一小写
def _normalize_and_validate_account(raw: str) -> str:
    """校验账号格式，返回规范化后的小写账号。不通过则 raise HTTPException。"""
    account = (raw or "").strip()
    if not account:
        raise HTTPException(status_code=400, detail="账号不能为空")
    if len(account) < 2 or len(account) > 64:
        raise HTTPException(status_code=400, detail="账号须 2～64 位")
    if not account[0].isalpha():
        raise HTTPException(status_code=400, detail="账号须字母开头")
    if not all(c.isalnum() or c in "._-" for c in account):
        raise HTTPException(status_code=400, detail="账号仅允许字母、数字、._-")
    return account.lower()


@router.get("/captcha", summary="获取图片验证码（登录/注册前调用，传输请使用 HTTPS）")
def get_captcha():
    """返回 captcha_id 与 data URI 图片，提交登录/注册时带上 captcha_id 与用户输入的 captcha_answer。
    生产环境必须使用 HTTPS，以保证验证码答案与密码在传输过程中加密。"""
    captcha_id, image_data_uri = create_captcha()
    return {"captcha_id": captcha_id, "image": image_data_uri}


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


async def get_messenger_user_id(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> int:
    """Messenger 多应用 CRUD：默认与 get_current_user 相同；海外机可设 messenger_trust_jwt_without_user=true，
    在库中无对应 users 行时仍采纳 JWT 的 sub（需与登录签发方共用 SECRET_KEY）。"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无法验证凭证",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError, TypeError):
        raise credentials_exception
    user = db.query(User).filter(User.id == user_id).first()
    if user is not None:
        return user_id
    if getattr(settings, "messenger_trust_jwt_without_user", False):
        return user_id
    raise credentials_exception


@router.post("/login", response_model=Token, summary="登录（表单含验证码，传输请使用 HTTPS）")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    captcha_id = (form.get("captcha_id") or "").strip()
    captcha_answer = (form.get("captcha_answer") or "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入账号和密码")
    if not verify_captcha(captcha_id, captcha_answer):
        raise HTTPException(status_code=400, detail="验证码错误或已过期，请刷新后重试")
    account_lower = _login_account_key(username)
    if not account_lower:
        raise HTTPException(status_code=400, detail="请输入账号和密码")
    user = db.query(User).filter(User.email == account_lower).first()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=400, detail="账号或密码错误")
    access_token = create_access_token(data=access_token_claims(user))
    iid = optional_installation_id_from_request(request)
    if iid:
        ensure_installation_slot(db, user.id, iid)
    return Token(access_token=access_token)


@router.post("/register", response_model=Token, summary="（已关闭）原字母账号注册，请用 /auth/register-phone")
def register(body: RegisterBody, request: Request, db: Session = Depends(get_db)):
    from ..core.config import settings
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    use_independent = getattr(settings, "lobster_independent_auth", True)
    if edition != "online" or not use_independent:
        raise HTTPException(status_code=400, detail="当前版本不支持自主注册")
    raise HTTPException(status_code=400, detail="已关闭账号密码注册，请使用手机号与短信验证码注册")


@router.post("/sms/send", summary="发送手机注册短信验证码（需先通过图形验证码）")
def send_register_sms(body: SmsSendBody, request: Request):
    from ..core.config import settings

    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    use_independent = getattr(settings, "lobster_independent_auth", True)
    if edition != "online" or not use_independent:
        raise HTTPException(status_code=400, detail="当前版本不支持")
    acc = (getattr(settings, "ihuyi_sms_account", None) or "").strip()
    pwd = (getattr(settings, "ihuyi_sms_password", None) or "").strip()
    if not acc or not pwd:
        raise HTTPException(status_code=503, detail="未配置短信通道（IHUYI_SMS_ACCOUNT / IHUYI_SMS_PASSWORD）")
    if not verify_captcha(body.captcha_id or "", body.captcha_answer or ""):
        raise HTTPException(status_code=400, detail="图形验证码错误或已过期，请刷新后重试")
    mobile = _normalize_cn_mobile(body.phone)
    now_m = time.monotonic()
    with _SMS_LOCK:
        _purge_sms_stale_locked(now_m)
        last = _SMS_SEND_AT.get(mobile, 0.0)
        if now_m - last < SMS_SEND_COOLDOWN_SEC:
            raise HTTPException(status_code=429, detail="发送过于频繁，请 1 分钟后再试")
        win = _SMS_SEND_HOUR_COUNT.get(mobile)
        if win:
            wstart, cnt = win
            if now_m - wstart > 3600:
                _SMS_SEND_HOUR_COUNT[mobile] = (now_m, 1)
            elif cnt >= SMS_MAX_PER_HOUR:
                raise HTTPException(status_code=429, detail="该号码本小时发送次数过多，请稍后再试")
            else:
                _SMS_SEND_HOUR_COUNT[mobile] = (wstart, cnt + 1)
        else:
            _SMS_SEND_HOUR_COUNT[mobile] = (now_m, 1)
        code = f"{random.randint(0, 999999):06d}"
        _SMS_CODE_STORE[mobile] = (code, now_m + SMS_CODE_TTL_SEC)
        _SMS_SEND_AT[mobile] = now_m
    try:
        send_verify_code_sms(account=acc, api_key=pwd, mobile=mobile, code=code)
    except RuntimeError as e:
        with _SMS_LOCK:
            _SMS_CODE_STORE.pop(mobile, None)
        raise HTTPException(status_code=502, detail=str(e)) from e
    logger.info("[auth/sms/send] mobile=%s ok=1", mobile[:3] + "****" + mobile[-4:])
    return {"ok": True}


@router.post("/register-phone", response_model=Token, summary="手机号注册（短信验证码 + 密码）")
def register_phone(body: RegisterPhoneBody, request: Request, db: Session = Depends(get_db)):
    from ..core.config import settings

    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    use_independent = getattr(settings, "lobster_independent_auth", True)
    if edition != "online" or not use_independent:
        raise HTTPException(status_code=400, detail="当前版本不支持自主注册")
    mobile = _normalize_cn_mobile(body.phone)
    code_in = (body.code or "").strip()
    if not code_in or len(code_in) > 8:
        raise HTTPException(status_code=400, detail="短信验证码无效")
    now_m = time.monotonic()
    with _SMS_LOCK:
        _purge_sms_stale_locked(now_m)
        row = _SMS_CODE_STORE.get(mobile)
        if not row or row[1] <= now_m or row[0] != code_in:
            raise HTTPException(status_code=400, detail="短信验证码错误或已过期，请重新获取")
        del _SMS_CODE_STORE[mobile]
    if len(body.password or "") < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    email = _phone_account_email(mobile)
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=400, detail="该手机号已注册，请直接登录")
    reg_iid = optional_installation_id_from_request(request)
    parent_uid = None
    raw_parent = (body.parent_account or "").strip()
    if raw_parent:
        parent_key = _login_account_key(raw_parent)
        parent_user = db.query(User).filter(User.email == parent_key).first() if parent_key else None
        if parent_user:
            parent_uid = parent_user.id
            logger.info("[auth/register-phone] parent_account=%s resolved parent_user_id=%s", raw_parent[:20], parent_uid)
        else:
            logger.warning("[auth/register-phone] parent_account=%s not found", raw_parent[:20])
    user = User(
        email=email,
        hashed_password=get_password_hash(body.password),
        credits=REGISTER_INITIAL_CREDITS,
        role="user",
        preferred_model="sutui",
        brand_mark=_normalize_brand_mark(body.brand_mark),
        parent_user_id=parent_uid,
    )
    db.add(user)
    db.flush()
    apply_installation_signup_bonus_for_new_user(db, user, reg_iid)
    db.commit()
    db.refresh(user)
    _remote = getattr(request.client, "host", None) if request.client else None
    _xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or None
    logger.info(
        "[auth/register-phone] user_id=%s mobile_tail=%s slots_enabled=%s installation_id_ok=%s credits=%s parent=%s remote=%s",
        user.id,
        mobile[-4:],
        installation_slots_enabled(),
        bool(reg_iid),
        user.credits,
        parent_uid,
        _remote,
        _xff,
    )
    for pkg_id in (
        "sutui_mcp",
        "douyin_publish",
        "xiaohongshu_publish",
        "toutiao_publish",
    ):
        db.add(SkillUnlock(user_id=user.id, package_id=pkg_id))
    db.commit()
    access_token = create_access_token(data=access_token_claims(user))
    if reg_iid:
        ensure_installation_slot(db, user.id, reg_iid)
    return Token(access_token=access_token)


@router.get("/me", response_model=UserOut, summary="当前用户信息")
def get_me(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    iid = optional_installation_id_from_request(request)
    if iid:
        ensure_installation_slot(db, current_user.id, iid)
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    preferred = "sutui" if edition == "online" else (getattr(current_user, "preferred_model", "openclaw") or "openclaw")
    return UserOut(
        id=current_user.id,
        email=current_user.email,
        preferred_model=preferred,
        credits=credits_json_float(getattr(current_user, "credits", None) or 0),
        brand_mark=getattr(current_user, "brand_mark", None),
        wecom_userid=getattr(current_user, "wecom_userid", None),
        is_agent=bool(getattr(current_user, "is_agent", False)),
    )


class WecomUserIdPatch(BaseModel):
    """企业微信回调中的发送者 ID（FromUserName），与当前登录账号绑定后，企微内发消息将按该账号扣费。"""
    wecom_userid: Optional[str] = None


@router.patch("/me/wecom-userid", summary="绑定/解绑企业微信 UserID（用于企微消息渠道扣费）")
def patch_me_wecom_userid(
    body: WecomUserIdPatch,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raw = (body.wecom_userid or "").strip()
    if raw:
        raw = raw[:128]
        other = (
            db.query(User)
            .filter(User.wecom_userid == raw, User.id != current_user.id)
            .first()
        )
        if other:
            raise HTTPException(status_code=400, detail="该企业微信 UserID 已被其他账号绑定")
        current_user.wecom_userid = raw
    else:
        current_user.wecom_userid = None
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return {"ok": True, "wecom_userid": current_user.wecom_userid}


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
            credits=DEFAULT_ONLINE_USER_CREDITS,
            role="user",
            preferred_model="sutui",
        )
        db.add(user)
        db.flush()
    user.sutui_token = api_key
    db.commit()
    access_token = create_access_token(data=access_token_claims(user))
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


# ── 自建微信登录（小程序码 + 轮询，流程类似速推）────────────────────────────────

def _use_own_wechat_login() -> bool:
    """是否启用自建微信登录（配置了 wechat_app_id + wechat_app_secret）。"""
    app_id = (getattr(settings, "wechat_app_id", None) or "").strip()
    secret = (getattr(settings, "wechat_app_secret", None) or "").strip()
    return bool(app_id and secret)


def _use_wechat_oa_login() -> bool:
    """是否启用服务号网页授权登录（配置了 wechat_oa_app_id + wechat_oa_secret）。"""
    app_id = (getattr(settings, "wechat_oa_app_id", None) or "").strip()
    secret = (getattr(settings, "wechat_oa_secret", None) or "").strip()
    return bool(app_id and secret)


# 小程序码扫码登录：scene_id -> token，5 分钟有效
_miniprogram_scene_store: Dict[str, Dict[str, Any]] = {}
_SCENE_TTL = 300  # 5 min

# 微信 access_token 缓存（2h 有效，提前 5min 刷新）
_wechat_access_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}


def _get_wechat_access_token() -> str:
    """获取小程序 access_token（用于生成小程序码）。"""
    now = time.time()
    if _wechat_access_token_cache["token"] and _wechat_access_token_cache["expires_at"] > now + 300:
        return _wechat_access_token_cache["token"]
    app_id = (getattr(settings, "wechat_app_id", None) or "").strip()
    app_secret = (getattr(settings, "wechat_app_secret", None) or "").strip()
    if not app_id or not app_secret:
        raise ValueError("未配置 wechat_app_id/wechat_app_secret")
    with httpx.Client(timeout=10.0) as client:
        r = client.get(
            "https://api.weixin.qq.com/cgi-bin/token",
            params={"grant_type": "client_credential", "appid": app_id, "secret": app_secret},
        )
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if data.get("errcode") or not data.get("access_token"):
        raise ValueError(data.get("errmsg") or "获取 access_token 失败")
    token = (data.get("access_token") or "").strip()
    expires_in = int(data.get("expires_in") or 7200)
    _wechat_access_token_cache["token"] = token
    _wechat_access_token_cache["expires_at"] = now + expires_in
    return token


def _wechat_oa_base_url(request: Request) -> str:
    base = (getattr(settings, "wechat_oa_base_url", None) or "").strip().rstrip("/")
    if base:
        return base
    return (get_effective_public_base_url() or str(request.base_url)).rstrip("/")


@router.get("/wechat-login-url", summary="自建微信：服务号返回 login_url，小程序返回 scene_id+qr_base64")
def get_wechat_login_url(request: Request):
    """优先服务号：若配置 wechat_oa_app_id，返回 login_url 供前端生成二维码；否则走小程序码。"""
    if _use_wechat_oa_login():
        app_id = (getattr(settings, "wechat_oa_app_id", None) or "").strip()
        base = _wechat_oa_base_url(request)
        redirect_uri = f"{base}/auth/wechat-callback"
        url = (
            "https://open.weixin.qq.com/connect/oauth2/authorize"
            f"?appid={quote(app_id, safe='')}"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
            "&response_type=code"
            "&scope=snsapi_userinfo"
            "&state=login"
            "#wechat_redirect"
        )
        logger.info("[wechat-login-url] 服务号 login_url base=%s", base)
        return {"login_url": url}
    if not _use_own_wechat_login():
        raise HTTPException(status_code=400, detail="未配置自建微信登录（wechat_oa_app_id/wechat_oa_secret 或 wechat_app_id/wechat_app_secret）")
    # 前端仅支持服务号 login_url；未配服务号时直接报错，避免返回小程序码导致「未返回链接」
    logger.warning("[wechat-login-url] 未配置服务号，请设置 WECHAT_OA_APP_ID、WECHAT_OA_SECRET")
    raise HTTPException(
        status_code=503,
        detail="请在服务器 .env 中配置服务号：WECHAT_OA_APP_ID、WECHAT_OA_SECRET、WECHAT_OA_BASE_URL（公众平台 基本配置 里获取 AppID/AppSecret）",
    )


@router.get("/wechat-miniprogram-login-status", summary="轮询：扫码后是否已登录，已登录返回 access_token")
def wechat_miniprogram_login_status(scene_id: Optional[str] = None):
    """前端轮询。若该 scene_id 已在小程序内完成登录，返回 status=ok 与 access_token。"""
    if not scene_id or not scene_id.strip():
        raise HTTPException(status_code=400, detail="缺少 scene_id")
    scene_id = scene_id.strip()
    entry = _miniprogram_scene_store.get(scene_id)
    if not entry:
        return {"status": "waiting"}
    if entry["expires_at"] < time.time():
        _miniprogram_scene_store.pop(scene_id, None)
        return {"status": "waiting"}
    if not entry.get("token"):
        return {"status": "waiting"}
    token = entry["token"]
    _miniprogram_scene_store.pop(scene_id, None)
    return {"status": "ok", "access_token": token}


class WechatMiniprogramLoginBody(BaseModel):
    code: str
    scene_id: str


@router.post("/wechat-miniprogram-login", summary="小程序内调用：code + scene_id 换 openid，绑定 scene 并返回 Token")
def wechat_miniprogram_login(
    body: WechatMiniprogramLoginBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """用户扫码进入小程序后，小程序带 scene，调 wx.login 得 code，再调此接口。后端用 code 换 openid，查/建用户，把 token 绑定到 scene_id，网页轮询即可拿到 token 完成登录。"""
    if not _use_own_wechat_login():
        raise HTTPException(status_code=400, detail="未配置小程序（wechat_app_id/wechat_app_secret）")
    js_code = (body.code or "").strip()
    scene_id = (body.scene_id or "").strip()
    if not js_code:
        raise HTTPException(status_code=400, detail="缺少 code")
    if not scene_id:
        raise HTTPException(status_code=400, detail="缺少 scene_id")
    app_id = (getattr(settings, "wechat_app_id", None) or "").strip()
    app_secret = (getattr(settings, "wechat_app_secret", None) or "").strip()
    with httpx.Client(timeout=10.0) as client:
        r = client.get(
            "https://api.weixin.qq.com/sns/jscode2session",
            params={
                "appid": app_id,
                "secret": app_secret,
                "js_code": js_code,
                "grant_type": "authorization_code",
            },
        )
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if data.get("errcode"):
        errmsg = data.get("errmsg") or "小程序登录失败"
        logger.warning("jscode2session err: %s", data)
        raise HTTPException(status_code=400, detail=errmsg)
    openid = (data.get("openid") or "").strip()
    if not openid:
        raise HTTPException(status_code=400, detail="未获取到 openid")
    user = db.query(User).filter(User.wechat_openid == openid).first()
    if not user:
        slots = installation_slots_enabled()
        if slots:
            hdr = request.headers.get(INSTALLATION_ID_HEADER) or request.headers.get("x-installation-id")
            wx_iid = parse_installation_id_strict(hdr)
        else:
            wx_iid = None
        email = f"wx_{openid[:16]}@wechat.lobster.local"
        if db.query(User).filter(User.email == email).first():
            email = f"wx_{openid}@wechat.lobster.local"
        user = User(
            email=email,
            hashed_password=get_password_hash(f"wechat-{openid}"),
            credits=REGISTER_INITIAL_CREDITS,
            role="user",
            preferred_model="sutui",
            wechat_openid=openid,
        )
        db.add(user)
        db.flush()
        apply_installation_signup_bonus_for_new_user(db, user, wx_iid if slots else None)
        db.commit()
        db.refresh(user)
    access_token = create_access_token(data=access_token_claims(user))
    entry = _miniprogram_scene_store.get(scene_id)
    if entry and entry["expires_at"] > time.time():
        entry["token"] = access_token
    return Token(access_token=access_token, token_type="bearer")


@router.get("/wechat-callback", summary="自建微信：服务号/网页授权扫码登录回调")
def wechat_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """微信回调带 code，用 code 换 openid，查/建用户并下发 JWT，重定向到 /?token=。优先用服务号 appid/secret。"""
    if not (code or "").strip():
        raise HTTPException(status_code=400, detail="缺少 code 参数")
    if _use_wechat_oa_login():
        app_id = (getattr(settings, "wechat_oa_app_id", None) or "").strip()
        app_secret = (getattr(settings, "wechat_oa_secret", None) or "").strip()
    elif _use_own_wechat_login():
        app_id = (getattr(settings, "wechat_app_id", None) or "").strip()
        app_secret = (getattr(settings, "wechat_app_secret", None) or "").strip()
    else:
        raise HTTPException(status_code=400, detail="未配置自建微信登录")
    # 与授权时一致的 redirect_uri，换 token 时部分场景要求一致
    base = _wechat_oa_base_url(request) if _use_wechat_oa_login() else (get_effective_public_base_url() or str(request.base_url)).rstrip("/")
    redirect_uri = f"{base.rstrip('/')}/auth/wechat-callback"
    with httpx.Client(timeout=10.0) as client:
        r = client.get(
            "https://api.weixin.qq.com/sns/oauth2/access_token",
            params={
                "appid": app_id,
                "secret": app_secret,
                "code": code.strip(),
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
    try:
        # 微信可能返回 text/plain，不依赖 Content-Type，直接解析 body
        data = r.json() if (r.text and r.text.strip()) else {}
    except Exception:
        data = {}
    openid = (data.get("openid") or "").strip()
    if not openid:
        errcode = data.get("errcode", "")
        errmsg = data.get("errmsg") or data.get("error_description") or ""
        err = (errmsg or "微信授权失败").strip()
        logger.warning(
            "wechat_callback no openid: status=%s body=%s redirect_uri=%s",
            r.status_code, r.text[:500] if r.text else "", redirect_uri,
        )
        # 返回可读错误，避免前端编码乱码；常见 errcode 40029=code无效/已用 43101=redirect_uri 不一致
        detail = f"errcode={errcode}: {err}" if errcode else err
        raise HTTPException(status_code=400, detail=detail)
    user = db.query(User).filter(User.wechat_openid == openid).first()
    if not user:
        slots = installation_slots_enabled()
        wx_iid = optional_installation_id_from_request(request)
        email = f"wx_{openid[:16]}@wechat.lobster.local"
        if db.query(User).filter(User.email == email).first():
            email = f"wx_{openid}@wechat.lobster.local"
        user = User(
            email=email,
            hashed_password=get_password_hash(f"wechat-{openid}"),
            credits=REGISTER_INITIAL_CREDITS,
            role="user",
            preferred_model="sutui",
            wechat_openid=openid,
        )
        db.add(user)
        db.flush()
        apply_installation_signup_bonus_for_new_user(db, user, wx_iid if slots else None)
        db.commit()
        db.refresh(user)
    access_token = create_access_token(data=access_token_claims(user))
    # 跳转到前端地址并带 token，前端通过 ?token= 自动登录（见 init.js applyTokenFromUrl）
    front_base = (getattr(settings, "wechat_oa_frontend_url", None) or "").strip().rstrip("/")
    if not front_base and _use_wechat_oa_login():
        front_base = (_wechat_oa_base_url(request) or "").rstrip("/")
    if not front_base:
        front_base = (get_effective_public_base_url() or str(request.base_url)).rstrip("/")
    from urllib.parse import urlparse
    has_query = bool(urlparse(front_base).query)
    redirect_url = f"{front_base}{'&' if has_query else '?'}token={quote(access_token, safe='')}"
    return RedirectResponse(url=redirect_url, status_code=302)


@router.get("/wechat-success", summary="自建微信：登录成功页（展示 Token，供本地盒子用户复制）")
def wechat_success(token: Optional[str] = None):
    """扫码登录成功后跳转至此页。有 PUBLIC_BASE_URL 时从 wechat-callback 跳来。展示 Token 供本地无公网前端用户复制。"""
    from fastapi.responses import HTMLResponse
    if not token or not token.strip():
        return HTMLResponse(
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body><p>缺少 token，请重新扫码登录。</p></body></html>",
            status_code=400,
        )
    t = token.strip()
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>登录成功</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:480px;margin:2rem auto;padding:1rem;}"
        ".token{word-break:break-all;background:#eee;padding:0.75rem;border-radius:6px;margin:0.5rem 0;font-size:0.9rem;}"
        "button{margin-top:0.5rem;padding:0.5rem 1rem;cursor:pointer;}</style></head><body>"
        "<h2>登录成功</h2>"
        "<p>若您使用<strong>本地应用</strong>（无公网），请复制下方 Token 到本地应用中粘贴完成登录：</p>"
        "<div class='token' id='tok'>" + t.replace("<", "&lt;") + "</div>"
        "<button onclick=\"navigator.clipboard.writeText(document.getElementById('tok').innerText);this.textContent='已复制'\">复制 Token</button>"
        "<p style='margin-top:1.5rem;color:#666;font-size:0.9rem'>若您从本服务器打开前端，可<a href='/?token=" + t + "'>点击此处</a>自动完成登录。</p>"
        "</body></html>"
    )
    return HTMLResponse(html)


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
            credits=DEFAULT_ONLINE_USER_CREDITS,
            role="user",
            preferred_model="sutui",
        )
        db.add(user)
        db.flush()
    user.sutui_token = api_key
    db.commit()
    access_token = create_access_token(data=access_token_claims(user))
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


# ── 代理商 API ────────────────────────────────────────────────────────

@router.get("/agent/sub-users", summary="代理商：查看下级用户列表与充值情况")
def agent_sub_users(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not getattr(current_user, "is_agent", False):
        raise HTTPException(status_code=403, detail="非代理商，无权访问")
    from sqlalchemy import func
    from ..models import RechargeOrder
    subs = db.query(User).filter(User.parent_user_id == current_user.id).order_by(User.created_at.desc()).all()
    result = []
    for u in subs:
        paid_sum = (
            db.query(func.coalesce(func.sum(RechargeOrder.credits), 0))
            .filter(RechargeOrder.user_id == u.id, RechargeOrder.status == "paid")
            .scalar()
        )
        result.append({
            "id": u.id,
            "email": u.email,
            "credits": credits_json_float(getattr(u, "credits", None) or 0),
            "total_recharged": int(paid_sum or 0),
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })
    return {"sub_users": result, "count": len(result)}
