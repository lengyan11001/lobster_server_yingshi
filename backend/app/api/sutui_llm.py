"""速推 LLM 探测结果只读接口。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends

from mcp.sutui_tokens import next_sutui_server_token

from ..models import User
from ..services.sutui_llm_probe import (
    _fetch_mcp_models,
    filter_chat_models_for_api,
    read_sutui_llm_snapshot,
)
from .auth import get_current_user

router = APIRouter()


def _pick_recommended_chat(models: List[Dict[str, Any]], previous: object) -> Optional[str]:
    """在仅含对话类的列表中选推荐：优先沿用快照推荐（若仍在列表内），否则优先 available，再取首项。"""
    ids = [str(m.get("id") or "").strip() for m in models if m.get("id")]
    if not ids:
        return None
    prev = str(previous or "").strip()
    if prev and prev in ids:
        return prev
    for m in models:
        if m.get("available") is True:
            return str(m.get("id") or "").strip() or None
    return ids[0]


@router.get("/api/sutui-llm/models", summary="速推对话 LLM 列表（仅 text/llm 类，需登录）")
async def get_sutui_llm_models(current_user: User = Depends(get_current_user)):
    """
    仅返回对话用 LLM（mcp/models 中 category 为 llm 或 text），不含生图/生视频等。
    优先读 data/sutui_llm_snapshot.json 再按上述规则过滤；过滤后为空则实时拉取 mcp/models 再过滤。
    """
    snap = read_sutui_llm_snapshot()
    raw_list = list(snap.get("models") or []) if isinstance(snap.get("models"), list) else []
    models = filter_chat_models_for_api(raw_list)
    probed_at = snap.get("probed_at")
    recommended = snap.get("recommended")
    error = snap.get("error")
    live_fill = False

    if not models:
        try:
            token = await next_sutui_server_token()
            if not token:
                raise RuntimeError("速推 Token 未配置，无法拉取 LLM 列表")
            raw = await _fetch_mcp_models(token)
            filtered = filter_chat_models_for_api(raw)
            out = []
            for m in filtered:
                if not isinstance(m, dict):
                    continue
                mid = (m.get("id") or "").strip()
                if not mid:
                    continue
                out.append(
                    {
                        "id": mid,
                        "name": (m.get("name") or m.get("title") or mid),
                        "category": m.get("category"),
                        "available": True,
                    }
                )
            if out:
                models = out
                live_fill = True
                error = None
        except Exception as e:
            if not error:
                error = str(e)[:800]

    recommended = _pick_recommended_chat(models, recommended)

    return {
        "probed_at": probed_at,
        "recommended": recommended,
        "models": models,
        "error": error,
        "category_filter": "llm,text",
        "live_fill": live_fill,
    }
