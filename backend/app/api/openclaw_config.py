"""OpenClaw Gateway configuration: status check, API key management, model selection, restart."""
import json
import logging
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import get_current_user
from ..models import User

logger = logging.getLogger(__name__)

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_OC_DIR = _BASE_DIR / "openclaw"
_OC_CONFIG = _OC_DIR / "openclaw.json"
_OC_ENV = _OC_DIR / ".env"

SUPPORTED_PROVIDERS = [
    {"id": "anthropic", "name": "Anthropic", "env_key": "ANTHROPIC_API_KEY",
     "models": ["anthropic/claude-sonnet-4-5", "anthropic/claude-opus-4-6", "anthropic/claude-haiku-3-5"]},
    {"id": "openai", "name": "OpenAI", "env_key": "OPENAI_API_KEY",
     "models": ["openai/gpt-4o", "openai/gpt-4o-mini", "openai/o3-mini"]},
    {"id": "deepseek", "name": "DeepSeek", "env_key": "DEEPSEEK_API_KEY",
     "models": ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"]},
    {"id": "google", "name": "Google", "env_key": "GEMINI_API_KEY",
     "models": ["google/gemini-2.5-pro", "google/gemini-2.5-flash"]},
]

DEEPSEEK_PROVIDER_TEMPLATE = {
    "baseUrl": "https://api.deepseek.com",
    "api": "openai-completions",
    "models": [
        {"id": "deepseek-chat", "name": "DeepSeek Chat", "input": ["text"],
         "contextWindow": 65536, "maxTokens": 8192},
        {"id": "deepseek-reasoner", "name": "DeepSeek Reasoner", "reasoning": True,
         "input": ["text"], "contextWindow": 65536, "maxTokens": 8192},
    ],
}


def _mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return ""
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def _read_oc_env() -> dict[str, str]:
    result: dict[str, str] = {}
    if not _OC_ENV.exists():
        return result
    for line in _OC_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _write_oc_env(data: dict[str, str]):
    _OC_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("# OpenClaw LLM API Keys")
    lines.append("# 在龙虾后台设置后自动写入")
    for k, v in sorted(data.items()):
        lines.append(f"{k}={v}")
    _OC_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_oc_config() -> dict:
    if not _OC_CONFIG.exists():
        return {}
    try:
        return json.loads(_OC_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_oc_config(config: dict):
    _OC_DIR.mkdir(parents=True, exist_ok=True)
    _OC_CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _model_to_agent_id(model: str) -> str:
    """Slugify a model ID into an OpenClaw agent ID."""
    slug = model.lower().replace("/", "-").replace(".", "-")
    slug = re.sub(r'[^a-z0-9_-]', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug[:64] or "main"


def _build_agents_list(primary_model: str) -> list[dict]:
    """Build the agents.list array from SUPPORTED_PROVIDERS.

    The default agent ('main') uses the primary model.
    Every supported model also gets a dedicated agent so switching the primary
    later never leaves a model without an agent entry.
    """
    agents = [{"id": "main", "default": True}]
    seen: set[str] = set()
    for prov in SUPPORTED_PROVIDERS:
        for model_id in prov["models"]:
            if model_id in seen:
                continue
            seen.add(model_id)
            agents.append({"id": _model_to_agent_id(model_id), "model": model_id})
    return agents


def _ensure_provider_configs(config: dict):
    """Dynamically add/remove non-built-in providers based on actual API key values.

    Uses the real key value in openclaw.json (not ${ENV_VAR} templates) to avoid
    OpenClaw SecretRef startup failures when keys are empty.
    """
    env_data = _read_oc_env()
    providers = config.setdefault("models", {}).setdefault("providers", {})

    ds_key = env_data.get("DEEPSEEK_API_KEY", "").strip()
    if ds_key:
        ds_cfg = dict(DEEPSEEK_PROVIDER_TEMPLATE)
        ds_cfg["apiKey"] = ds_key
        providers["deepseek"] = ds_cfg
    else:
        providers.pop("deepseek", None)

    if not providers:
        config.get("models", {}).pop("providers", None)
        if not config.get("models"):
            config.pop("models", None)


def _ensure_agents_list(config: dict):
    """Ensure agents.list contains an agent for every supported model."""
    agents_node = config.setdefault("agents", {})
    primary = agents_node.get("defaults", {}).get("model", {}).get("primary", _DEFAULT_PRIMARY)
    agents_node["list"] = _build_agents_list(primary)


_DEFAULT_PRIMARY = "anthropic/claude-sonnet-4-5"


@router.get("/api/openclaw/status", summary="OpenClaw Gateway 状态")
async def openclaw_status(current_user: User = Depends(get_current_user)):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("http://127.0.0.1:18789/")
        return {"online": True, "status_code": r.status_code}
    except Exception:
        return {"online": False, "status_code": None}


@router.get("/api/openclaw/config", summary="读取 OpenClaw 配置")
def get_openclaw_config(current_user: User = Depends(get_current_user)):
    env_data = _read_oc_env()
    config = _read_oc_config()

    primary_model = ""
    try:
        primary_model = (
            config.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
            or config.get("agent", {}).get("model", {}).get("primary", "")
        )
    except Exception:
        pass

    providers_status = []
    for p in SUPPORTED_PROVIDERS:
        raw_key = env_data.get(p["env_key"], "")
        providers_status.append({
            "id": p["id"],
            "name": p["name"],
            "env_key": p["env_key"],
            "configured": bool(raw_key),
            "masked_key": _mask_key(raw_key),
            "models": p["models"],
        })

    return {
        "primary_model": primary_model,
        "providers": providers_status,
    }


class UpdateOpenClawConfig(BaseModel):
    primary_model: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None


@router.post("/api/openclaw/config", summary="更新 OpenClaw 配置")
def update_openclaw_config(
    body: UpdateOpenClawConfig,
    current_user: User = Depends(get_current_user),
):
    env_data = _read_oc_env()
    changed_keys = False

    key_map = {
        "ANTHROPIC_API_KEY": body.anthropic_api_key,
        "OPENAI_API_KEY": body.openai_api_key,
        "DEEPSEEK_API_KEY": body.deepseek_api_key,
        "GEMINI_API_KEY": body.gemini_api_key,
    }
    for env_key, value in key_map.items():
        if value is not None:
            env_data[env_key] = value.strip()
            changed_keys = True

    if changed_keys:
        _write_oc_env(env_data)

    config = _read_oc_config()

    if body.primary_model is not None:
        config.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})["primary"] = body.primary_model.strip()

    _ensure_provider_configs(config)
    _ensure_agents_list(config)
    _write_oc_config(config)

    restarted = False
    if changed_keys:
        restarted = _restart_openclaw_gateway()

    msg = "配置已保存"
    if restarted:
        msg += "，OpenClaw Gateway 已自动重启。"
    elif changed_keys:
        msg += "。API Key 已更新，但自动重启失败，请手动重启（stop.bat + start.bat）。"
    else:
        msg += "。"

    return {"ok": True, "message": msg, "restarted": restarted}


def _find_openclaw_pid() -> Optional[int]:
    """Find the PID of the process listening on port 18789."""
    try:
        if platform.system() == "Windows":
            out = subprocess.check_output(
                'netstat -ano | findstr ":18789 " | findstr "LISTENING"',
                shell=True, text=True, stderr=subprocess.DEVNULL,
            )
            for line in out.strip().splitlines():
                parts = line.split()
                if parts:
                    return int(parts[-1])
        else:
            out = subprocess.check_output(
                ["lsof", "-ti", ":18789"], text=True, stderr=subprocess.DEVNULL,
            )
            pid_str = out.strip().splitlines()[0] if out.strip() else ""
            if pid_str.isdigit():
                return int(pid_str)
    except Exception:
        pass
    return None


def _kill_pid(pid: int):
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, 9)
    except Exception as e:
        logger.warning("Failed to kill PID %s: %s", pid, e)


def _build_openclaw_env() -> dict:
    """Build environment variables for the OpenClaw child process."""
    env = dict(os.environ)
    oc_env = _read_oc_env()
    env.update(oc_env)
    env["OPENCLAW_CONFIG_PATH"] = str(_OC_CONFIG)
    env["OPENCLAW_STATE_DIR"] = str(_OC_DIR)
    return env


def _find_openclaw_entry() -> Optional[tuple]:
    """Find node executable and openclaw.mjs path. Returns (node_path, mjs_path) or None."""
    base = _BASE_DIR

    node_candidates = [
        base / "nodejs" / "node.exe",
        base / "nodejs" / "node",
    ]
    node_path = None
    for p in node_candidates:
        if p.exists():
            node_path = str(p)
            break
    if not node_path:
        import shutil
        node_path = shutil.which("node")

    mjs_candidates = [
        base / "nodejs" / "node_modules" / "openclaw" / "openclaw.mjs",
        base / "node_modules" / "openclaw" / "openclaw.mjs",
    ]
    mjs_path = None
    for p in mjs_candidates:
        if p.exists():
            mjs_path = str(p)
            break

    if node_path and mjs_path:
        return (node_path, mjs_path)
    return None


def _restart_openclaw_gateway() -> bool:
    """Kill existing OpenClaw process and start a new one with fresh env. Returns True on success."""
    old_pid = _find_openclaw_pid()
    if old_pid:
        logger.info("Killing old OpenClaw Gateway PID %s", old_pid)
        _kill_pid(old_pid)
        time.sleep(1)

    entry = _find_openclaw_entry()
    if not entry:
        logger.warning("Cannot restart OpenClaw: node or openclaw.mjs not found")
        return False

    node_path, mjs_path = entry
    env = _build_openclaw_env()
    log_path = _BASE_DIR / "openclaw.log"

    try:
        cmd = [node_path, mjs_path, "gateway", "--port", "18789"]
        log_file = None
        for _ in range(2):
            try:
                log_file = open(log_path, "a", encoding="utf-8")
                break
            except OSError as e:
                if getattr(e, "errno", None) == 13:  # Permission denied (file locked by previous process)
                    time.sleep(0.5)
                    continue
                raise
        if log_file is None:
            logger.warning("openclaw.log locked, OpenClaw stdout/stderr will not be written to file")
            log_file = subprocess.DEVNULL

        kwargs = {
            "stdout": log_file,
            "stderr": log_file,
            "env": env,
            "cwd": str(_BASE_DIR),
        }
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        subprocess.Popen(cmd, **kwargs)
        logger.info("OpenClaw Gateway restarting: %s", " ".join(cmd))
        time.sleep(2)

        new_pid = _find_openclaw_pid()
        if new_pid:
            logger.info("OpenClaw Gateway restarted, PID %s", new_pid)
            return True
        else:
            logger.warning("OpenClaw Gateway process started but not listening yet")
            return True
    except Exception as e:
        logger.error("Failed to restart OpenClaw Gateway: %s", e)
        return False


@router.post("/api/openclaw/restart", summary="重启 OpenClaw Gateway")
async def restart_openclaw(current_user: User = Depends(get_current_user)):
    ok = _restart_openclaw_gateway()
    if ok:
        return {"ok": True, "message": "OpenClaw Gateway 已重启"}
    return {"ok": False, "message": "重启失败，请手动执行 stop.bat + start.bat"}


# --------------- SuTui MCP Config ---------------

_SUTUI_CONFIG_PATH = _BASE_DIR / "sutui_config.json"
_UPSTREAM_URLS_PATH = _BASE_DIR / "upstream_urls.json"
_SUTUI_DEFAULT_URL = "https://api.xskill.ai/api/v3/mcp-http"


def _read_sutui_config() -> dict:
    if not _SUTUI_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_SUTUI_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_sutui_config(data: dict):
    _SUTUI_CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _read_upstream_urls() -> dict:
    if not _UPSTREAM_URLS_PATH.exists():
        return {}
    try:
        return json.loads(_UPSTREAM_URLS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_upstream_urls(data: dict):
    _UPSTREAM_URLS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


@router.get("/api/sutui/config", summary="读取速推配置")
def get_sutui_config(current_user: User = Depends(get_current_user)):
    from ..core.config import settings
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    urls = _read_upstream_urls()
    url = urls.get("sutui", _SUTUI_DEFAULT_URL)
    if edition == "online":
        token = (getattr(current_user, "sutui_token", None) or "").strip()
        return {"token": _mask_key(token) if token else "", "has_token": bool(token), "url": url, "edition": "online"}
    cfg = _read_sutui_config()
    token = cfg.get("token", "")
    return {
        "token": _mask_key(token) if token else "",
        "has_token": bool(token),
        "url": url,
    }


class UpdateSutuiConfig(BaseModel):
    token: Optional[str] = None
    url: Optional[str] = None


@router.post("/api/sutui/config", summary="保存速推配置")
def update_sutui_config(
    body: UpdateSutuiConfig,
    current_user: User = Depends(get_current_user),
):
    from ..core.config import settings
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition == "online":
        if body.token is not None:
            raise HTTPException(400, detail="在线版 Token 由速推登录提供，无需在此配置")
    cfg = _read_sutui_config()
    if body.token is not None and edition != "online":
        cfg["token"] = body.token.strip()
    _write_sutui_config(cfg)

    if body.url is not None and body.url.strip():
        urls = _read_upstream_urls()
        urls["sutui"] = body.url.strip()
        _write_upstream_urls(urls)
    elif not _read_upstream_urls().get("sutui"):
        urls = _read_upstream_urls()
        urls["sutui"] = _SUTUI_DEFAULT_URL
        _write_upstream_urls(urls)

    return {"ok": True, "message": "速推配置已保存"}


@router.get("/api/sutui/balance", summary="在线版：速推余额（仅 online 有效）")
def get_sutui_balance(current_user: User = Depends(get_current_user)):
    from ..core.config import settings
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition != "online":
        raise HTTPException(status_code=400, detail="仅在线版支持")
    api_key = (getattr(current_user, "sutui_token", None) or "").strip()
    if not api_key:
        return {"balance": 0, "balance_yuan": "0.00", "vip_level": 0, "error": "未绑定速推账号"}
    base = (getattr(settings, "sutui_api_base", None) or "https://api.xskill.ai").rstrip("/")
    url = f"{base}/api/v3/balance"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params={"api_key": api_key})
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if data.get("code") != 200:
            return {"balance": 0, "balance_yuan": "0.00", "vip_level": 0, "error": data.get("detail") or data.get("msg") or "获取失败"}
        d = data.get("data") or {}
        return {
            "balance": d.get("balance", 0),
            "balance_yuan": d.get("balance_yuan", "0.00"),
            "vip_level": d.get("vip_level", 0),
            "user_id": d.get("user_id"),
        }
    except Exception as e:
        logger.warning("sutui balance request failed: %s", e)
        return {"balance": 0, "balance_yuan": "0.00", "vip_level": 0, "error": "网络错误"}


# --------------- 速推充值（对接速推真实接口：get_pay_info_list / create_wx_order_info）---------------

_XSKILL_RECHARGE_URL = "https://www.xskill.ai/#/cn-recharge"
_CUSTOM_CONFIGS_FILE = _BASE_DIR / "custom_configs.json"


def _default_recharge_shops():
    """默认充值档位（当 get_pay_info_list 失败时使用）。"""
    return [
        {"shop_id": 0, "money_yuan": 100, "title": "100 元"},
        {"shop_id": 0, "money_yuan": 500, "title": "500 元"},
        {"shop_id": 0, "money_yuan": 1000, "title": "1000 元"},
    ]


def _get_custom_recharge_tiers() -> Optional[list]:
    """从 custom_configs.json 读取 RECHARGE_TIERS，用于自定义展示的档位、顺序和文案。
    格式: configs.RECHARGE_TIERS.shops = [ { \"shop_id\": 73, \"label\": \"1000元 推荐\", \"money_yuan\": 1000 }, ... ]。
    注意：实际支付金额由速推侧 shop_id 决定，label/money_yuan 仅用于展示；shop_id 需与速推商品一致。"""
    if not _CUSTOM_CONFIGS_FILE.exists():
        return None
    try:
        data = json.loads(_CUSTOM_CONFIGS_FILE.read_text(encoding="utf-8"))
        cfg = (data.get("configs") or {}).get("RECHARGE_TIERS")
        if not isinstance(cfg, dict):
            return None
        shops = cfg.get("shops")
        if not isinstance(shops, list) or not shops:
            return None
        out = []
        for s in shops:
            if not isinstance(s, dict):
                continue
            sid = s.get("shop_id")
            if sid is None:
                continue
            label = (s.get("label") or s.get("title") or "").strip() or f"{s.get('money_yuan', 0)} 元"
            money_yuan = s.get("money_yuan")
            if money_yuan is None:
                money_yuan = s.get("money")
                if isinstance(money_yuan, (int, float)) and money_yuan > 100:
                    money_yuan = money_yuan / 1000.0
            out.append({"shop_id": int(sid), "title": label, "money_yuan": float(money_yuan) if money_yuan is not None else 0, "tag": s.get("tag") or ""})
        return out if out else None
    except Exception as e:
        logger.debug("RECHARGE_TIERS read failed: %s", e)
        return None


@router.get("/api/sutui/recharge-options", summary="在线版：充值选项（速推 get_pay_info_list，可被 RECHARGE_TIERS 覆盖）")
def get_sutui_recharge_options(current_user: User = Depends(get_current_user)):
    from ..core.config import settings
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition != "online":
        raise HTTPException(status_code=400, detail="仅在线版支持")
    api_key = (getattr(current_user, "sutui_token", None) or "").strip()
    custom = _get_custom_recharge_tiers()
    if not api_key:
        if custom is not None:
            return {"shops": custom, "recharge_url": _XSKILL_RECHARGE_URL, "custom": True}
        return {"shops": _default_recharge_shops(), "recharge_url": _XSKILL_RECHARGE_URL}
    base = (getattr(settings, "sutui_api_base", None) or "https://api.xskill.ai").rstrip("/")
    url = f"{base}/api/get_pay_info_list"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json={"token": api_key, "shop_type": 10})
        if resp.status_code != 200:
            return {"shops": custom or _default_recharge_shops(), "recharge_url": _XSKILL_RECHARGE_URL, "custom": custom is not None}
        raw = resp.json()
        if not isinstance(raw, list):
            return {"shops": custom or _default_recharge_shops(), "recharge_url": _XSKILL_RECHARGE_URL, "custom": custom is not None}
        id_to_yuan = {}
        shops = []
        for item in raw:
            sid = item.get("id")
            money_yuan = item.get("money_yuan") or (item.get("money", 0) / 1000.0 if item.get("money") else 0)
            title = (item.get("title") or "").strip() or f"{money_yuan} 元"
            if sid is not None and money_yuan > 0:
                id_to_yuan[sid] = money_yuan
                shops.append({"shop_id": sid, "money_yuan": money_yuan, "title": title, "tag": item.get("tag") or ""})
        if not shops:
            return {"shops": custom or _default_recharge_shops(), "recharge_url": _XSKILL_RECHARGE_URL, "custom": custom is not None}
        if custom is not None:
            for c in custom:
                if not c.get("money_yuan"):
                    c["money_yuan"] = id_to_yuan.get(c["shop_id"], 0)
            return {"shops": custom, "recharge_url": _XSKILL_RECHARGE_URL, "custom": True}
        shops.sort(key=lambda x: x["money_yuan"])
        return {"shops": shops, "recharge_url": _XSKILL_RECHARGE_URL}
    except Exception as e:
        logger.debug("sutui get_pay_info_list fallback: %s", e)
        return {"shops": custom or _default_recharge_shops(), "recharge_url": _XSKILL_RECHARGE_URL, "custom": custom is not None}


class RechargeCreateBody(BaseModel):
    shop_id: int
    amount_yuan: Optional[float] = None


@router.post("/api/sutui/recharge-create", summary="在线版：创建充值订单（速推 create_wx_order_info，需 JWT 时返回官网链接）")
def create_sutui_recharge(
    body: RechargeCreateBody,
    current_user: User = Depends(get_current_user),
):
    from ..core.config import settings
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition != "online":
        raise HTTPException(status_code=400, detail="仅在线版支持")
    api_key = (getattr(current_user, "sutui_token", None) or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="未绑定速推账号，请先扫码登录")
    base = (getattr(settings, "sutui_api_base", None) or "https://api.xskill.ai").rstrip("/")
    url = f"{base}/api/create_wx_order_info"
    payload = {"token": api_key, "shop_id": body.shop_id}
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code == 500 and (data.get("detail") or "").find("token") >= 0:
            return {
                "need_oauth": True,
                "recharge_url": _XSKILL_RECHARGE_URL,
                "message": "充值需在速推官网完成登录后支付，请前往官网充值",
            }
        if resp.status_code != 200 or data.get("code") != 200:
            msg = data.get("detail") or data.get("msg") or "创建订单失败"
            raise HTTPException(status_code=400, detail=msg)
        d = data.get("data") or data
        pay_url = (d.get("pay_url") or d.get("payment_url") or d.get("code_url") or "").strip()
        qr_code = (d.get("qr_code") or d.get("code_url") or "").strip() if not pay_url else ""
        order_id = str(d.get("out_trade_no") or d.get("order_id") or d.get("id") or "")
        return {
            "pay_url": pay_url or None,
            "qr_code": qr_code or None,
            "order_id": order_id,
            "need_oauth": False,
            "recharge_url": _XSKILL_RECHARGE_URL,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("sutui recharge-create failed: %s", e)
        return {
            "need_oauth": True,
            "recharge_url": _XSKILL_RECHARGE_URL,
            "message": "调用失败，请前往官网充值",
        }
