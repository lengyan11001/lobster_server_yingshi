"""
速推 /api/v3/tasks/create 等 REST 错误文案增强：根据 HTTP 状态、响应体与能力类型补充「下次怎么用」的中文提示。

模型 ID 集合来自对 https://api.xskill.ai/api/v3/mcp/models + 各 model docs 的离线扫描
（见 lobster_online/scripts/_sutui_models_schema_scan.json）；若官方增删图像模型，请同步更新下方 IMAGE_MODEL_IDS。
"""

from __future__ import annotations

import json
from typing import Any, Optional

# category=image（速推 MCP 模型表 2026-03 扫描）。勿用于 video.generate。
IMAGE_MODEL_IDS = frozenset(
    {
        "fal-ai/bytedance/seedream/v4.5/edit",
        "fal-ai/bytedance/seedream/v4.5/text-to-image",
        "fal-ai/bytedance/seedream/v5/lite/edit",
        "fal-ai/bytedance/seedream/v5/lite/text-to-image",
        "fal-ai/flux-2/flash",
        "fal-ai/nano-banana-2",
        "fal-ai/nano-banana-pro",
        "fal-ai/qwen-image-edit-2511-multiple-angles",
        "jimeng-4.0",
        "jimeng-4.1",
        "jimeng-4.5",
        "jimeng-4.6",
        "jimeng-5.0",
        "jimeng-agent",
        "kapon/gemini-3-pro-image-preview",
        "openrouter/router/vision",
    }
)


def _norm_model(s: Any) -> str:
    return str(s or "").strip()


def _json_detail_snippets(text: str) -> str:
    """尽量从 FastAPI/Pydantic 风格 JSON 里抽出可读片段。"""
    t = (text or "").strip()
    if not t or t[0] not in "{[":
        return ""
    try:
        obj = json.loads(t)
    except Exception:
        return ""
    parts: list[str] = []

    def walk(x: Any, prefix: str = "") -> None:
        if len(parts) >= 6:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("loc", "ctx"):
                    continue
                if isinstance(v, (dict, list)):
                    walk(v, f"{prefix}{k}.")
                else:
                    parts.append(f"{prefix}{k}={v}")
        elif isinstance(x, list):
            for i, it in enumerate(x[:8]):
                walk(it, f"{prefix}[{i}].")

    walk(obj)
    return "；".join(parts)[:600]


def hint_for_wrong_capability_model(capability_id: str, model: str) -> Optional[str]:
    """能力类型与模型类别明显不一致时的固定说明。"""
    mid = _norm_model(model)
    if not mid:
        return None
    if capability_id == "video.generate" and mid in IMAGE_MODEL_IDS:
        return (
            f"当前 model「{mid}」在速推侧属于「图像」模型，不能用于 video.generate。"
            "请改用 invoke_capability + capability_id=image.generate 生成图片；"
            "若要做视频，请换「视频」类 model_id（如含 seedance、sora、wan、veo、kling、图生视频/文生视频 等）。"
        )
    if capability_id == "image.generate":
        low = mid.lower()
        if "image-to-video" in low or "text-to-video" in low:
            return (
                f"当前 model「{mid}」从命名上看是「视频」任务（含 image-to-video / text-to-video），"
                "不应使用 image.generate。请改用 capability_id=video.generate，并按该模型文档提供 duration、aspect_ratio 或首帧图等参数。"
            )
        if "/seedance/" in low and "video" in low:
            return (
                f"当前 model「{mid}」属于 Seedance「视频」链路，请使用 capability_id=video.generate，不要用 image.generate。"
            )
    return None


def enhance_upstream_rest_error(
    *,
    http_status: int,
    err_body: str,
    capability_id: str,
    model: str,
) -> str:
    """
    在「上游 REST HTTP xxx: …」基础上追加排查提示；不改变原始错误前缀，便于日志检索。
    """
    raw = (err_body or "")[:800]
    base = f"上游 REST HTTP {http_status}: {raw}"
    extra: list[str] = []

    h0 = hint_for_wrong_capability_model(capability_id, model)
    if h0:
        extra.append(h0)

    blob = f"{err_body or ''}"
    low = blob.lower()
    js = _json_detail_snippets(blob)

    if http_status == 422:
        extra.append(
            "HTTP 422 表示「参数与模型要求不一致」。请对照速推该模型的 params_schema："
            "GET /api/v3/models/{model_id}/docs?lang=zh（将 model_id 做 URL 编码）。"
        )
        if any(k in low for k in ("prompt", "required", "field required", "missing")):
            extra.append("若提示缺少 prompt：请在 payload 中填写非空 prompt（文生图/文生视频通常必填）。")
        if any(k in low for k in ("image_url", "image_urls", "首帧", "reference")):
            extra.append(
                "若提示图片相关字段：图生视频/参考图类模型需要可公网访问的 image_url 或 image_urls；"
                "在线版请优先使用「本条消息附图」或素材库公网 URL，勿填素材内部 ID。"
            )
        if any(k in low for k in ("num_images", "less_than_equal", "greater than", "maximum")):
            extra.append(
                "若提示 num_images/n 超上限：多数图模单批 1～4 张（Seedream 部分为 1～6）；请把 num_images 改小后重试。"
            )
        if "aspect_ratio" in low or "aspect ratio" in low:
            extra.append(
                "若提示 aspect_ratio：请使用该模型 docs 里 enum 列出的取值（如 nano-banana-2 支持 auto、16:9、9:16 等；"
                "勿传模型不认识的字符串）。"
            )
        if "duration" in low:
            extra.append(
                "若提示 duration：不同视频模型要求整数秒、或带 s 后缀字符串、或固定枚举；请查该模型 docs 的示例。"
            )
        if js:
            extra.append(f"接口返回细节摘要：{js}")

    elif http_status == 400:
        if "model" in low and ("invalid" in low or "not found" in low or "unknown" in low):
            extra.append("请核对 model 是否为速推 /api/v3/mcp/models 中的 id（区分大小写与路径）。")

    seen: set[str] = set()
    merged: list[str] = []
    for p in extra:
        p = p.strip()
        if not p or p in seen:
            continue
        seen.add(p)
        merged.append(p)

    if not merged:
        return base
    return base + "\n\n【排查提示】" + "\n".join(f"• {x}" for x in merged)


def append_capability_model_hint(message: str, capability_id: str, model: str) -> str:
    """在任意上游错误文案后追加「能力/模型是否匹配」类提示（不改变原前缀）。"""
    h = hint_for_wrong_capability_model(capability_id, model)
    if not h:
        return message
    return message + "\n\n【排查提示】\n• " + h
