"""定时拉取速推 LLM 清单并探测 chat/completions 可用性，写入 data/sutui_llm_snapshot.json。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from mcp.sutui_tokens import next_sutui_server_token

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DATA_DIR = _ROOT / "data"
_SNAPSHOT_PATH = _DATA_DIR / "sutui_llm_snapshot.json"


def snapshot_path() -> Path:
    return _SNAPSHOT_PATH


def is_sutui_llm_probe_enabled_for_this_instance() -> bool:
    """仅国内主实例执行定时探测。海外 lobster_server 请在环境变量中设置 LOBSTER_SERVER_REGION=overseas。"""
    region = (os.environ.get("LOBSTER_SERVER_REGION") or "").strip().lower()
    if region == "overseas":
        return False
    return True


def _api_base() -> str:
    return (os.environ.get("SUTUI_API_BASE") or "https://api.xskill.ai").rstrip("/")


def _filter_models_by_category(raw_models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """默认仅对话类：category=llm 或 text（速推 mcp/models 常将对话模型标为 text，无 llm 标签）。
    设 SUTUI_LLM_PROBE_CATEGORY=all 或 * 表示不按分类过滤。"""
    cat_raw = (os.environ.get("SUTUI_LLM_PROBE_CATEGORY") or "llm").strip().lower()
    out: List[Dict[str, Any]] = []
    for m in raw_models:
        if not isinstance(m, dict):
            continue
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        if cat_raw in ("all", "*", ""):
            out.append(dict(m))
            continue
        c = str(m.get("category", "")).strip().lower()
        if c == cat_raw:
            out.append(dict(m))
            continue
        # 默认筛「llm」时同步纳入 text，否则仅设默认 llm、未设 SUTUI_LLM_PROBE_CATEGORY 的实例会得到空列表
        if cat_raw == "llm" and c == "text":
            out.append(dict(m))
    return out


def filter_chat_models_for_api(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """会话「速推 LLM」下拉：仅 category 为 llm 或 text，排除 image/video/audio 等生成类。"""
    out: List[Dict[str, Any]] = []
    for m in entries:
        if not isinstance(m, dict):
            continue
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        c = str(m.get("category", "")).strip().lower()
        if c in ("llm", "text"):
            out.append(m)
    return out


async def _fetch_mcp_models(token: str) -> List[Dict[str, Any]]:
    base = _api_base()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.get(f"{base}/api/v3/mcp/models", headers=headers)
    r.raise_for_status()
    body = r.json()
    if not isinstance(body, dict):
        raise RuntimeError("mcp/models 响应不是 JSON 对象")
    data = body.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("mcp/models 缺少 data 对象")
    models = data.get("models")
    if not isinstance(models, list):
        raise RuntimeError("mcp/models data.models 不是数组")
    return models


async def _probe_one_chat(token: str, model_id: str) -> tuple[bool, int]:
    """返回 (是否 200 可用, HTTP 状态码)。402 表示管理端 Token 池余额不足，非模型不可用。"""
    base = _api_base()
    url = f"{base}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(url, json=body, headers=headers)
        if r.status_code == 200:
            return True, r.status_code
        if r.status_code == 402:
            logger.warning(
                "[sutui_llm_probe] model=%s status=402 管理端 Token 余额不足，探测将判不可用（请充值后重试）",
                model_id,
            )
        else:
            logger.info("[sutui_llm_probe] model=%s status=%s body=%s", model_id, r.status_code, (r.text or "")[:300])
        return False, r.status_code
    except Exception as e:
        logger.info("[sutui_llm_probe] model=%s error=%s", model_id, e)
        return False, 0


def _pick_recommended(candidates: List[Dict[str, Any]]) -> Optional[str]:
    env_pref = (os.environ.get("SUTUI_LLM_RECOMMENDED_ID") or "").strip()
    ids = [str(c.get("id", "")).strip() for c in candidates if c.get("available")]
    if not ids:
        return None
    if env_pref and env_pref in ids:
        return env_pref
    return ids[0]


async def run_sutui_llm_probe_once() -> Dict[str, Any]:
    """执行一次探测并写入快照文件（仅国内实例；使用默认速推 Token 池）。"""
    if not is_sutui_llm_probe_enabled_for_this_instance():
        raise RuntimeError("本实例未启用速推 LLM 探测（海外机请设 LOBSTER_SERVER_REGION=overseas，且不应执行探测）")

    token = await next_sutui_server_token()
    if not token:
        raise RuntimeError(
            "速推 Token 未配置，无法探测 LLM（需 SUTUI_SERVER_TOKENS_USER / SUTUI_SERVER_TOKEN 或兼容项 "
            "SUTUI_SERVER_TOKEN / SUTUI_SERVER_TOKENS）"
        )

    raw_models = await _fetch_mcp_models(token)
    filtered = _filter_models_by_category(raw_models)
    cat_disp = (os.environ.get("SUTUI_LLM_PROBE_CATEGORY") or "llm").strip()

    if not filtered:
        raise RuntimeError(
            f"速推 mcp/models 在当前分类规则下无模型（SUTUI_LLM_PROBE_CATEGORY={cat_disp!r}）。"
            "可设为 all 探测全部条目后再收窄。"
        )

    # 只对对话类做 chat 探测；此前 SUTUI_LLM_PROBE_CATEGORY=all 时会对生图/生视频打 completions，易全 503 且浪费额度
    to_probe = filter_chat_models_for_api(filtered)
    if not to_probe:
        raise RuntimeError(
            "速推 mcp/models 在分类规则下无 text/llm 对话模型，无法探测（请检查上游 catalog）。"
        )

    prev_good: Optional[Dict[str, Any]] = None
    if _SNAPSHOT_PATH.exists():
        try:
            prev = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
            if sum(1 for x in (prev.get("models") or []) if x.get("available")) > 0:
                prev_good = prev
        except Exception:
            pass

    out_models: List[Dict[str, Any]] = []
    saw_402 = False
    for m in to_probe:
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        ok, code = await _probe_one_chat(token, mid)
        if code == 402:
            saw_402 = True
        await asyncio.sleep(0.15)
        entry = {
            "id": mid,
            "name": (m.get("name") or m.get("title") or mid),
            "category": m.get("category"),
            "available": ok,
        }
        out_models.append(entry)

    candidates = [x for x in out_models if x.get("available")]
    recommended = _pick_recommended(candidates)

    if not candidates and saw_402 and prev_good is not None:
        logger.warning(
            "[sutui_llm_probe] 本轮探测因 402 余额不足全部不可用，保留上次有效快照不覆盖 path=%s",
            _SNAPSHOT_PATH,
        )
        return prev_good

    ts = datetime.now(timezone.utc).isoformat()
    payload: Dict[str, Any] = {
        "probed_at": ts,
        "api_base": _api_base(),
        "category_filter": cat_disp,
        "recommended": recommended,
        "models": out_models,
    }
    if not candidates and saw_402:
        payload["error"] = "探测时管理端 Token 返回 402 余额不足，本轮无可用模型；请充值后等待下次探测或查看实时接口。"
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SNAPSHOT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "[sutui_llm_probe] 完成 total=%s available=%s recommended=%s",
        len(out_models),
        len(candidates),
        recommended,
    )
    return payload


def read_sutui_llm_snapshot() -> Dict[str, Any]:
    """读取上次快照；文件不存在时返回空结构。"""
    if not _SNAPSHOT_PATH.exists():
        return {
            "probed_at": None,
            "recommended": None,
            "models": [],
            "error": "尚未完成探测",
        }
    try:
        return json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return {"probed_at": None, "recommended": None, "models": [], "error": str(e)}


async def sutui_llm_probe_loop_forever(interval_sec: float = 3600.0) -> None:
    """后台每小时执行一次（启动后先立即跑一次）。仅在国内实例由 create_app 启动。"""
    while True:
        try:
            await run_sutui_llm_probe_once()
        except Exception:
            logger.exception("[sutui_llm_probe] 探测失败")
        await asyncio.sleep(interval_sec)
