"""
video.generate 的 payload.model 统一解析：展示名 / 误填 id / 速推短 id → 速推真实 model id。

单一入口 resolve_video_model_id，避免在 http_server 里零散「一个个加」别名。
与 docs/视频生成-按模型参数转换.md 对齐；速推模型清单见 scripts/_sutui_models_schema_scan.json。
"""

from __future__ import annotations

import re
from typing import Dict, Tuple

# (文生视频 id, 图生视频 id)
Pair = Tuple[str, str]


def _p(t2v: str, i2v: str) -> Pair:
    return (t2v, i2v)


def _norm_key(s: str) -> str:
    """小写 + 空白压成单空格，便于别名表命中。"""
    return " ".join((s or "").strip().lower().split())


def _norm_key_compact(s: str) -> str:
    return _norm_key(s).replace(" ", "")


def _pick(pair: Pair, has_image: bool) -> str:
    return pair[1] if has_image else pair[0]


# ---------------------------------------------------------------------------
# 展示名 / 口语 → (t2v, i2v)。键两种形态：带空格、无空格。
# ---------------------------------------------------------------------------
def _build_alias_map() -> Dict[str, Pair]:
    m: Dict[str, Pair] = {}

    def add(names: Tuple[str, ...], pair: Pair) -> None:
        for n in names:
            k1 = _norm_key(n)
            k2 = _norm_key_compact(n)
            if k1:
                m[k1] = pair
            if k2:
                m[k2] = pair

    # —— Sora 2（默认 Pub 档；与旧启发式一致）——
    sora_pub = _p("fal-ai/sora-2/text-to-video", "fal-ai/sora-2/image-to-video")
    sora_vip = _p("fal-ai/sora-2/vip/text-to-video", "fal-ai/sora-2/vip/image-to-video")
    sora_pro = _p("fal-ai/sora-2/text-to-video/pro", "fal-ai/sora-2/image-to-video/pro")

    add(
        (
            "sora 2",
            "sora2",
            "sora-2",
            "sora 2 default",
            "openai sora 2",
            "openai sora2",
        ),
        sora_pub,
    )
    add(
        (
            "sora 2 pub",
            "sora2pub",
            "sora2 pub",
            "sora pub",
            "sora2 pub 文生视频",
            "sora2 pub 图生视频",
        ),
        sora_pub,
    )
    add(
        (
            "sora 2 vip",
            "sora2vip",
            "sora2 vip",
            "sora vip",
        ),
        sora_vip,
    )
    add(
        (
            "sora 2 pro",
            "sora2pro",
            "sora2 pro",
            "sora pro",
        ),
        sora_pro,
    )

    # —— Seedance / super-seed2 ——
    sd2 = _p("st-ai/super-seed2", "st-ai/super-seed2")
    add(
        (
            "seedance 2",
            "seedance2",
            "seedance 2.0",
            "seedance2.0",
            "super seed2",
            "super-seed2",
            "superseed2",
            "seedance2 文生",
        ),
        sd2,
    )

    # —— Wan 2.6 ——
    wan26 = _p("wan/v2.6/text-to-video", "wan/v2.6/image-to-video")
    add(
        (
            "wan 2.6",
            "wan2.6",
            "wan v2.6",
        ),
        wan26,
    )

    # —— Wan 2.7 ——
    wan27 = _p("wan/v2.7/text-to-video", "wan/v2.7/image-to-video")
    add(
        (
            "wan 2.7",
            "wan2.7",
            "wan v2.7",
        ),
        wan27,
    )

    # 未指定版本的 wan → 默认 v2.7（最新可用）
    add(
        (
            "万相",
            "wan 视频",
            "wan",
        ),
        wan27,
    )

    # —— Veo 3.1 ——（速推 ID 为 fal-ai/veo3.1，不区分 t2v/i2v）
    veo = _p("fal-ai/veo3.1", "fal-ai/veo3.1")
    add(
        (
            "veo 3.1",
            "veo3.1",
            "veo3",
            "google veo",
            "veo",
        ),
        veo,
    )

    # —— Kling（默认 O3 Pro；与旧代码默认偏 pro 对齐）——
    kling = _p(
        "fal-ai/kling-video/o3/pro/text-to-video",
        "fal-ai/kling-video/o3/pro/image-to-video",
    )
    add(
        (
            "kling",
            "kling o3",
            "kling video",
            "可灵",
            "可灵 o3",
        ),
        kling,
    )

    # —— Grok ——
    grok = _p("xai/grok-imagine-video/text-to-video", "xai/grok-imagine-video/image-to-video")
    add(("grok", "grok imagine", "grok video", "grok imagine video"), grok)

    # —— 海螺 / Hailuo 2.3 ——
    hailuo = _p("fal-ai/minimax/hailuo-2.3/pro/text-to-video", "fal-ai/minimax/hailuo-2.3/pro/image-to-video")
    add(
        (
            "hailuo",
            "hailuo 2.3",
            "hailuo2.3",
            "海螺",
            "海螺视频",
            "minimax hailuo",
        ),
        hailuo,
    )

    hailuo_std = _p("fal-ai/minimax/hailuo-2.3/standard/text-to-video", "fal-ai/minimax/hailuo-2.3/standard/image-to-video")
    add(("hailuo standard", "hailuo 标准", "海螺标准"), hailuo_std)

    hailuo_fast = _p("fal-ai/minimax/hailuo-2.3-fast/pro/image-to-video", "fal-ai/minimax/hailuo-2.3-fast/pro/image-to-video")
    add(("hailuo fast", "hailuo 快速", "海螺快速"), hailuo_fast)

    # —— Vidu Q3 ——
    vidu = _p("fal-ai/vidu/q3/text-to-video", "fal-ai/vidu/q3/image-to-video")
    add(("vidu", "vidu q3", "vidu3"), vidu)

    # —— 即梦视频 ——（id 无斜杠，整串透传也可，别名便于口语）
    jm = _p("jimeng-video-3.5-pro", "jimeng-video-3.5-pro")
    add(("即梦视频", "即梦 视频", "jimeng video", "jimeng-video"), jm)

    return m


ALIAS_MAP: Dict[str, Pair] = _build_alias_map()

# 速推扫描表里的短 id：与 fal-ai 长 id 等价（REST 侧常认长 id）
_LEGACY_PREFIX_REWRITE: Tuple[Tuple[str, str], ...] = (
    ("sora2pub/", "fal-ai/sora-2/"),
)


def _rewrite_legacy_prefix(model: str) -> str:
    m = model
    low = m.lower()
    for old, new in _LEGACY_PREFIX_REWRITE:
        if low.startswith(old):
            return new + m[len(old) :]
    return m


# LLM 幻觉 / 残缺 ID → 正确模型
_BAD_SUBSTR_REWRITE: Tuple[Tuple[re.Pattern[str], Pair], ...] = (
    (re.compile(r"^pb-movie", re.I), _p("fal-ai/sora-2/text-to-video", "fal-ai/sora-2/image-to-video")),
    # LLM 只写了 "standard/image-to-video" 或 "pro/text-to-video" 等残缺路径（实为海螺子路径）
    (re.compile(r"^standard/(?:image|text)-to-video$", re.I),
     _p("fal-ai/minimax/hailuo-2.3/standard/text-to-video", "fal-ai/minimax/hailuo-2.3/standard/image-to-video")),
    (re.compile(r"^pro/(?:image|text)-to-video$", re.I),
     _p("fal-ai/minimax/hailuo-2.3/pro/text-to-video", "fal-ai/minimax/hailuo-2.3/pro/image-to-video")),
    # 纯 "image-to-video" / "text-to-video"（无模型名）
    (re.compile(r"^(?:image|text)-to-video$", re.I),
     _p("fal-ai/minimax/hailuo-2.3/pro/text-to-video", "fal-ai/minimax/hailuo-2.3/pro/image-to-video")),
)


def _canonical_prefixes() -> Tuple[str, ...]:
    return (
        "fal-ai/",
        "st-ai/",
        "wan/",
        "sprcra/",
        "xai/",
        "ark/",
        "openrouter/",
        "sora2pub/",
    )


def _looks_like_canonical_id(m: str) -> bool:
    low = m.lower()
    if any(low.startswith(p) for p in _canonical_prefixes()):
        return True
    # 无斜杠但已是速推常用 id
    if low.startswith("jimeng-video") or low.startswith("jimeng-"):
        return True
    return False


def _heuristic_video_model(model: str, has_image: bool) -> str:
    """无斜杠或非标展示名时的分支规则（由原 http_server 迁入，保持行为）。"""
    model_lower = model.lower()
    if "sora" in model_lower and ("2" in model or "pub" in model_lower or "vip" in model_lower or "pro" in model_lower):
        if "pub" in model_lower:
            return "fal-ai/sora-2/image-to-video" if has_image else "fal-ai/sora-2/text-to-video"
        if "vip" in model_lower:
            return "fal-ai/sora-2/vip/image-to-video" if has_image else "fal-ai/sora-2/vip/text-to-video"
        if "pro" in model_lower:
            return "fal-ai/sora-2/pro/image-to-video" if has_image else "fal-ai/sora-2/text-to-video/pro"
        return "fal-ai/sora-2/image-to-video" if has_image else "fal-ai/sora-2/text-to-video"

    if "seedance" in model_lower or ("seed" in model_lower and "seedream" not in model_lower):
        if "2" in model or "2.0" in model:
            return "st-ai/super-seed2"
        if "1.5" in model:
            if "text" in model_lower or "t2v" in model_lower:
                return "fal-ai/bytedance/seedance/v1.5/pro/text-to-video"
            return (
                "fal-ai/bytedance/seedance/v1.5/pro/image-to-video"
                if has_image
                else "fal-ai/bytedance/seedance/v1.5/pro/text-to-video"
            )
        if "1" in model and "1.5" not in model:
            if "fast" in model_lower:
                return (
                    "fal-ai/bytedance/seedance/v1/pro/fast/image-to-video"
                    if has_image
                    else "fal-ai/bytedance/seedance/v1/pro/fast/text-to-video"
                )
            if "lite" in model_lower:
                if "reference" in model_lower or "ref" in model_lower:
                    return "fal-ai/bytedance/seedance/v1/lite/reference-to-video"
                return (
                    "fal-ai/bytedance/seedance/v1/lite/image-to-video"
                    if has_image
                    else "fal-ai/bytedance/seedance/v1/lite/text-to-video"
                )
            return (
                "fal-ai/bytedance/seedance/v1/pro/image-to-video"
                if has_image
                else "fal-ai/bytedance/seedance/v1/pro/text-to-video"
            )

    if "kling" in model_lower:
        if "o3" in model_lower and "pro" in model_lower:
            return (
                "fal-ai/kling-video/o3/pro/image-to-video"
                if has_image
                else "fal-ai/kling-video/o3/pro/text-to-video"
            )
        if "o3" in model_lower:
            return (
                "fal-ai/kling-video/o3/image-to-video"
                if has_image
                else "fal-ai/kling-video/o3/text-to-video"
            )
        return (
            "fal-ai/kling-video/image-to-video"
            if has_image
            else "fal-ai/kling-video/text-to-video"
        )

    if "wan" in model_lower or "万" in model:
        if "2.6" in model:
            return "wan/v2.6/image-to-video" if has_image else "wan/v2.6/text-to-video"
        return "wan/v2.7/image-to-video" if has_image else "wan/v2.7/text-to-video"

    if "veo" in model_lower:
        return "fal-ai/veo3.1"

    if "grok" in model_lower:
        return (
            "xai/grok-imagine-video/image-to-video"
            if has_image
            else "xai/grok-imagine-video/text-to-video"
        )

    if "hailuo" in model_lower or "海螺" in model:
        if "fast" in model_lower or "快速" in model:
            return "fal-ai/minimax/hailuo-2.3-fast/pro/image-to-video"
        if "standard" in model_lower or "标准" in model:
            return (
                "fal-ai/minimax/hailuo-2.3/standard/image-to-video"
                if has_image
                else "fal-ai/minimax/hailuo-2.3/standard/text-to-video"
            )
        return (
            "fal-ai/minimax/hailuo-2.3/pro/image-to-video"
            if has_image
            else "fal-ai/minimax/hailuo-2.3/pro/text-to-video"
        )

    if "vidu" in model_lower:
        return (
            "fal-ai/vidu/q3/image-to-video"
            if has_image
            else "fal-ai/vidu/q3/text-to-video"
        )

    if "即梦" in model or "jimeng" in model_lower:
        return "jimeng-video-3.5-pro"

    return model


def resolve_video_model_id(raw: str, has_image: bool) -> str:
    """
    将 LLM/用户填入的 model 解析为速推可接受的视频模型 id。
    has_image：是否视为图生视频（filePaths/image_url/media_files 已有图）。
    """
    m = (raw or "").strip()
    if not m:
        return m

    m = _rewrite_legacy_prefix(m)

    # 别名表（展示名）
    nk = _norm_key(m)
    nk2 = _norm_key_compact(m)
    if nk in ALIAS_MAP:
        return _pick(ALIAS_MAP[nk], has_image)
    if nk2 in ALIAS_MAP:
        return _pick(ALIAS_MAP[nk2], has_image)

    low = m.lower()

    # 子串级误填（已知幻觉 id）
    for rx, pair in _BAD_SUBSTR_REWRITE:
        if rx.search(low):
            return _pick(pair, has_image)

    # 已是标准 id：直接透传（rewrite 后 sora2pub 已变 fal-ai/sora-2/...）
    if _looks_like_canonical_id(m):
        return m

    # 其余：展示名 / 无前缀 / 非白名单斜杠 id → 启发式
    return _heuristic_video_model(m, has_image)
