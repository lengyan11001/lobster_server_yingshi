"""Comfly 中转平台上游调用模块。

与速推(xSkill)并行的生成能力上游。当模型在 comfly_pricing.json 中配置了定价时，
invoke_capability 将路由到 Comfly 而非速推。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from collections import OrderedDict

logger = logging.getLogger("lobster.mcp.comfly")

_PRICING_PATH = Path(__file__).resolve().parent.parent / "comfly_pricing.json"
_pricing_cache: Optional[Dict[str, Any]] = None
_pricing_mtime: float = 0

_MAX_COMFLY_TASK_TRACK = 5000
_comfly_task_ids: "OrderedDict[str, Tuple[str, str]]" = OrderedDict()


def register_comfly_task(task_id: str, token_group: str = "", api_format: str = "") -> None:
    """记录由 Comfly 创建的 task_id 及其 token_group 和 api_format，用于 task.get_result 路由。"""
    tid = (task_id or "").strip()
    if not tid:
        return
    _comfly_task_ids[tid] = (token_group or "", api_format or "")
    while len(_comfly_task_ids) > _MAX_COMFLY_TASK_TRACK:
        _comfly_task_ids.popitem(last=False)


def is_comfly_task(task_id: str) -> bool:
    """判断 task_id 是否属于 Comfly。"""
    return (task_id or "").strip() in _comfly_task_ids


def get_comfly_task_token_group(task_id: str) -> str:
    """获取 Comfly task 对应的 token_group。"""
    entry = _comfly_task_ids.get((task_id or "").strip())
    if entry is None:
        return ""
    return entry[0] if isinstance(entry, tuple) else entry


def get_comfly_task_api_format(task_id: str) -> str:
    """获取 Comfly task 对应的 api_format。"""
    entry = _comfly_task_ids.get((task_id or "").strip())
    if entry is None:
        return ""
    return entry[1] if isinstance(entry, tuple) else ""


def _load_pricing() -> Dict[str, Any]:
    """热加载 comfly_pricing.json（文件修改后自动刷新）。"""
    global _pricing_cache, _pricing_mtime
    try:
        mt = _PRICING_PATH.stat().st_mtime
    except FileNotFoundError:
        _pricing_cache = {"models": {}}
        return _pricing_cache
    if _pricing_cache is not None and mt == _pricing_mtime:
        return _pricing_cache
    try:
        raw = _PRICING_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        _pricing_cache = data if isinstance(data, dict) else {"models": {}}
        _pricing_mtime = mt
    except Exception as e:
        logger.warning("[Comfly] 加载 comfly_pricing.json 失败: %s", e)
        if _pricing_cache is None:
            _pricing_cache = {"models": {}}
    return _pricing_cache


def get_comfly_config(token_group: str = "") -> Tuple[str, str]:
    """返回 (base_url, api_key)。

    token_group 对应环境变量 COMFLY_API_KEY_<GROUP>（大写），
    未设置时回退到默认 COMFLY_API_KEY。
    base_url 会去除尾部 /v1 以避免与端点路径拼接时出现 /v1/v1 重复。
    """
    base = (os.environ.get("COMFLY_API_BASE") or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    key = ""
    if token_group:
        env_name = f"COMFLY_API_KEY_{token_group.upper()}"
        key = (os.environ.get(env_name) or "").strip()
        if key:
            logger.debug("[Comfly] 使用 token_group=%s env=%s", token_group, env_name)
    if not key:
        key = (os.environ.get("COMFLY_API_KEY") or "").strip()
    return base, key


def _get_model_token_group(model_id: str) -> str:
    """从 comfly_pricing.json 中查找模型的 token_group。"""
    entry = lookup_comfly_model(model_id)
    if entry and isinstance(entry, dict):
        return (entry.get("token_group") or "").strip()
    return ""


def is_comfly_configured() -> bool:
    base, key = get_comfly_config()
    return bool(base and key)


def lookup_comfly_model(model_id: str) -> Optional[Dict[str, Any]]:
    """查找模型是否在 Comfly 定价表中。返回定价条目或 None。
    支持直接按 Comfly 模型名查找，也支持通过 sutui_equivalent 反查。
    当精确匹配失败时，尝试前缀匹配（如 fal-ai/veo3.1/xxx 匹配 fal-ai/veo3.1）。
    """
    if not model_id:
        return None
    pricing = _load_pricing()
    models = pricing.get("models") or {}
    entry = models.get(model_id)
    if entry and isinstance(entry, dict):
        return entry
    low = model_id.lower()
    for k, v in models.items():
        if k.lower() == low:
            return v
    for _k, v in models.items():
        if not isinstance(v, dict):
            continue
        eq = v.get("sutui_equivalent")
        if isinstance(eq, list) and any(e.lower() == low for e in eq if isinstance(e, str)):
            return v
        if isinstance(eq, str) and eq.lower() == low:
            return v
    # Prefix fallback: fal-ai/veo3.1/lite → try fal-ai/veo3.1, then fal-ai
    parts = low.rsplit("/", 1)
    while len(parts) == 2 and parts[0]:
        prefix = parts[0]
        for k, v in models.items():
            if k.lower() == prefix:
                return v
        for _k, v in models.items():
            if not isinstance(v, dict):
                continue
            eq = v.get("sutui_equivalent")
            _eqs = eq if isinstance(eq, list) else ([eq] if isinstance(eq, str) else [])
            if any(e.lower() == prefix for e in _eqs if isinstance(e, str)):
                return v
        parts = prefix.rsplit("/", 1)
    return None


def should_route_to_comfly(capability_id: str, model_id: str, *, sutui_price: Optional[float] = None) -> bool:
    """判断是否应将请求路由到 Comfly。

    当模型在 Comfly 定价表中且满足以下条件之一时返回 True：
    1. 速推无此模型（sutui_price_per_unit 未配置且 sutui_price 未传入）
    2. Comfly 采购价 <= 速推采购价（比价路由）
    """
    if capability_id not in ("image.generate", "video.generate"):
        return False
    if not is_comfly_configured():
        return False
    entry = lookup_comfly_model(model_id)
    if not entry:
        return False
    comfly_price = entry.get("price_per_unit")
    if comfly_price is None:
        return True
    comfly_price = float(comfly_price)
    st_price = sutui_price if sutui_price is not None else entry.get("sutui_price_per_unit")
    if st_price is None:
        return True
    st_price = float(st_price)
    use_comfly = comfly_price <= st_price
    logger.info("[Comfly] 比价 model=%s comfly=%.1f sutui=%.1f → %s", model_id, comfly_price, st_price, "comfly" if use_comfly else "sutui")
    return use_comfly


def _user_price_multiplier() -> float:
    """用户实际消耗 = 采购价 × 倍率。优先取环境变量 COMFLY_USER_PRICE_MULTIPLIER，其次 JSON 配置。"""
    env_val = os.environ.get("COMFLY_USER_PRICE_MULTIPLIER", "").strip()
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    pricing = _load_pricing()
    return float(pricing.get("user_price_multiplier_default", 3))


def estimate_comfly_credits(model_id: str, params: Dict[str, Any], *, for_user: bool = False) -> Optional[int]:
    """按 Comfly 定价表估算算力消耗。for_user=True 时返回用户价（采购价 × 倍率）。

    支持的 price_type：
    - per_call：base = price_per_unit
    - per_second：base = price_per_unit × duration
    - per_token：base = (input_tokens/1000 × input_price + output_tokens/1000 × output_price)
                 estimate 时若 params 未提供实际 token，则按 prompt_tokens=5000, completion_tokens=1000 粗估。
    """
    entry = lookup_comfly_model(model_id)
    if not entry:
        return None
    price_type = (entry.get("price_type") or "per_call").strip()
    multiplier = float(entry.get("user_price_multiplier", _user_price_multiplier())) if for_user else 1.0

    if price_type == "per_token":
        input_unit = entry.get("input_price_per_1k_tokens")
        output_unit = entry.get("output_price_per_1k_tokens")
        if input_unit is None and output_unit is None:
            return None
        input_unit = float(input_unit or 0)
        output_unit = float(output_unit or 0)
        # 实际计费传 usage 时优先用真实 token 数；估算时给典型值
        usage = params.get("usage") if isinstance(params.get("usage"), dict) else None
        if usage:
            in_tok = float(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            out_tok = float(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        else:
            in_tok = float(params.get("estimate_prompt_tokens") or 5000)
            out_tok = float(params.get("estimate_completion_tokens") or 1000)
        base = (in_tok / 1000.0) * input_unit + (out_tok / 1000.0) * output_unit
        # per_token 价格非常小，避免 round 后变成 0；至少计 1 积分（按 multiplier 后）
        return max(1, int(round(base * multiplier))) if base > 0 else 0

    unit_price = entry.get("price_per_unit")
    if unit_price is None:
        return None
    unit_price = float(unit_price)
    if price_type == "per_second":
        duration = params.get("duration") or params.get("seconds") or 5
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = 5
        base = unit_price * duration
    else:
        base = unit_price
    return int(round(base * multiplier))


def get_all_comfly_pricing() -> Dict[str, Any]:
    """返回完整定价表（供 API 端点暴露给 lobster_online）。"""
    return _load_pricing()


# ---------------------------------------------------------------------------
# Comfly API 调用
# ---------------------------------------------------------------------------

async def call_comfly_image_generate(
    model_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """调用 Comfly 图片生成 API (DALL-E 格式)。"""
    tg = _get_model_token_group(model_id)
    base, key = get_comfly_config(tg)
    entry = lookup_comfly_model(model_id) or {}
    comfly_model = entry.get("comfly_model") or model_id

    prompt = (payload.get("prompt") or "").strip()
    body: Dict[str, Any] = {
        "model": comfly_model,
        "prompt": prompt,
        "n": payload.get("n") or 1,
    }
    size = payload.get("image_size") or payload.get("size")
    if size:
        body["size"] = size

    image_url = payload.get("image_url") or ""
    image_urls = payload.get("image_urls") or []
    if image_url:
        body["image"] = image_url
    elif image_urls and isinstance(image_urls, list) and image_urls:
        body["image"] = image_urls[0]

    api_format = (entry.get("api_format") or "dalle").strip()

    if api_format == "dalle":
        url = f"{base}/v1/images/generations"
        if body.get("image"):
            url = f"{base}/v1/images/edits"
    else:
        url = f"{base}/v1/images/generations"

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    logger.info("[Comfly] 图片生成请求 model=%s url=%s", comfly_model, url)
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(url, json=body, headers=headers)
        resp = r.json() if r.content else {}
        if r.status_code >= 400:
            err = resp.get("error", {})
            msg = err.get("message", str(resp)) if isinstance(err, dict) else str(err)
            return {"error": {"message": f"Comfly 返回 HTTP {r.status_code}: {msg}"}}
        return resp
    except Exception as e:
        logger.exception("[Comfly] 图片生成请求异常")
        return {"error": {"message": f"Comfly 请求失败: {e}"}}


async def call_comfly_video_generate(
    model_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """调用 Comfly 视频生成 API (统一格式)。"""
    tg = _get_model_token_group(model_id)
    base, key = get_comfly_config(tg)
    entry = lookup_comfly_model(model_id) or {}
    comfly_model = entry.get("comfly_model") or model_id
    api_format = (entry.get("api_format") or "unified_video").strip()

    prompt = (payload.get("prompt") or "").strip()
    _raw_dur = payload.get("duration") or payload.get("seconds") or 5
    try:
        if isinstance(_raw_dur, str) and _raw_dur.strip().lower().endswith("s"):
            duration = int(_raw_dur.strip().lower().rstrip("s"))
        else:
            duration = int(_raw_dur)
    except (ValueError, TypeError):
        duration = 5

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    image_url = payload.get("image_url") or ""
    file_paths = payload.get("filePaths") or []
    media_files = payload.get("media_files") or []
    first_image = image_url or (file_paths[0] if file_paths else "") or (media_files[0] if media_files else "")

    if api_format == "veo":
        url = f"{base}/v2/videos/generations"
        body: Dict[str, Any] = {
            "model": comfly_model,
            "prompt": prompt,
            "enhance_prompt": True,
        }
        aspect_ratio = payload.get("aspect_ratio") or "16:9"
        body["aspect_ratio"] = aspect_ratio
        if first_image:
            body["image_url"] = first_image
    elif api_format == "unified_video":
        if first_image:
            url = f"{base}/task/submit/i2v"
            body = {
                "model": comfly_model,
                "prompt": prompt,
                "image_url": first_image,
            }
        else:
            url = f"{base}/task/submit/t2v"
            body = {
                "model": comfly_model,
                "prompt": prompt,
            }
        if duration:
            body["duration"] = str(int(duration))
    elif api_format == "sora2":
        url = f"{base}/task/submit/t2v"
        body = {
            "model": comfly_model,
            "prompt": prompt,
        }
        if first_image:
            body["image_url"] = first_image
        if duration:
            body["duration"] = str(int(duration))
    else:
        url = f"{base}/task/submit/t2v"
        body = {
            "model": comfly_model,
            "prompt": prompt,
        }
        if first_image:
            body["image_url"] = first_image

    logger.info("[Comfly] 视频生成请求 model=%s url=%s api_format=%s body=%s", comfly_model, url, api_format, json.dumps(body, ensure_ascii=False)[:300])
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(url, json=body, headers=headers)
        resp = r.json() if r.content else {}
        logger.info(
            "[Comfly] 视频生成响应 HTTP=%s keys=%s preview=%s",
            r.status_code,
            list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__,
            str(resp)[:500],
        )
        if r.status_code >= 400:
            err = resp.get("error", {})
            msg = err.get("message", str(resp)) if isinstance(err, dict) else str(err)
            return {"error": {"message": f"Comfly 返回 HTTP {r.status_code}: {msg}"}}
        if isinstance(resp, dict):
            resp["_api_format"] = api_format
        return resp
    except Exception as e:
        logger.exception("[Comfly] 视频生成请求异常")
        return {"error": {"message": f"Comfly 请求失败: {e}"}}


async def call_comfly_task_query(task_id: str, token_group: str = "", api_format: str = "") -> Dict[str, Any]:
    """查询 Comfly 任务状态。api_format=veo 时用 /v2/videos/generations/{task_id}。"""
    base, key = get_comfly_config(token_group)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if api_format == "veo":
        url = f"{base}/v2/videos/generations/{task_id}"
    else:
        url = f"{base}/task/query/{task_id}"
    logger.info("[Comfly] 任务查询 task_id=%s url=%s api_format=%s", task_id, url, api_format or "(default)")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(url, headers=headers)
        resp = r.json() if r.content else {}
        logger.info(
            "[Comfly] 任务查询响应 HTTP=%s keys=%s preview=%s",
            r.status_code,
            list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__,
            str(resp)[:500],
        )
        return resp
    except Exception as e:
        logger.exception("[Comfly] 任务查询异常 task_id=%s", task_id)
        return {"error": {"message": f"Comfly 查询失败: {e}"}}


def format_comfly_image_response_as_sutui(resp: Dict[str, Any]) -> Dict[str, Any]:
    """将 Comfly DALL-E 格式响应转换为速推兼容格式（方便下游统一处理）。"""
    data_list = resp.get("data") or []
    if not data_list:
        return resp

    urls = []
    for item in data_list:
        u = item.get("url") or item.get("b64_json") or ""
        if u:
            urls.append(u)

    if not urls:
        return resp

    return {
        "task_id": resp.get("id") or f"comfly-img-{int(time.time())}",
        "status": "completed",
        "output": {
            "images": [{"url": u} for u in urls],
        },
        "url": urls[0],
        "_comfly": True,
    }


async def call_comfly_chat_completions(
    model_id: str,
    messages: List[Dict[str, Any]],
    *,
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
    stream: bool = False,
) -> Dict[str, Any]:
    """调用 Comfly /v1/chat/completions (OpenAI 兼容格式)。"""
    tg = _get_model_token_group(model_id)
    base, key = get_comfly_config(tg)
    if not base or not key:
        return {"error": {"message": "Comfly 未配置 (COMFLY_API_BASE / COMFLY_API_KEY)"}}

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    body: Dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    if stream:
        body["stream"] = True

    url = f"{base}/v1/chat/completions"
    logger.info("[Comfly] chat/completions 请求 model=%s url=%s", model_id, url)
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(url, json=body, headers=headers)
        resp = r.json() if r.content else {}
        if r.status_code >= 400:
            err = resp.get("error", {})
            msg = err.get("message", str(resp)) if isinstance(err, dict) else str(err)
            return {"error": {"message": f"Comfly chat 返回 HTTP {r.status_code}: {msg}"}}
        return resp
    except Exception as e:
        logger.exception("[Comfly] chat/completions 请求异常")
        return {"error": {"message": f"Comfly chat 请求失败: {e}"}}


def format_comfly_video_response_as_sutui(resp: Dict[str, Any]) -> Dict[str, Any]:
    """将 Comfly 视频响应转换为速推兼容格式。"""
    data = resp.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    task_id = (
        resp.get("task_id")
        or resp.get("taskId")
        or resp.get("id")
        or (data.get("task_id") if isinstance(data, dict) else "")
        or (data.get("taskId") if isinstance(data, dict) else "")
        or (data.get("id") if isinstance(data, dict) else "")
        or ""
    )
    raw_status = resp.get("status") or (data.get("status") if isinstance(data, dict) else "") or "pending"
    _VEO_STATUS_MAP = {
        "NOT_START": "pending",
        "IN_PROGRESS": "pending",
        "SUCCESS": "completed",
        "FAILURE": "failed",
    }
    status = _VEO_STATUS_MAP.get(raw_status, raw_status)

    if not task_id:
        logger.warning(
            "[Comfly] format_video: task_id 为空! resp_keys=%s data_keys=%s resp_preview=%s",
            list(resp.keys()),
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            str(resp)[:500],
        )

    result: Dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "_comfly": True,
    }

    video_url = resp.get("video_url") or resp.get("url") or ""
    if not video_url:
        out = resp.get("data") or resp.get("output") or {}
        if isinstance(out, dict):
            video_url = out.get("output") or out.get("video_url") or out.get("url") or ""

    if video_url:
        result["output"] = {"video_url": video_url}
        result["url"] = video_url
        result["status"] = "completed"

    if raw_status == "FAILURE":
        result["status"] = "failed"
        fail_reason = resp.get("fail_reason") or ""
        if fail_reason:
            result["output"] = {"error": fail_reason}

    return result
