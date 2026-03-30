"""速推 LLM 探测结果只读接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from mcp.sutui_tokens import next_sutui_server_token

from ..models import User
from ..services.sutui_llm_probe import (
    _fetch_mcp_models,
    _filter_models_by_category,
    read_sutui_llm_snapshot,
)
from .auth import get_current_user

router = APIRouter()


@router.get("/api/sutui-llm/models", summary="上次探测的速推 LLM 列表与推荐模型（需登录）")
async def get_sutui_llm_models(current_user: User = Depends(get_current_user)):
    """
    优先返回 data/sutui_llm_snapshot.json（国内定时探测）。
    若快照不存在或 models 为空，则实时拉取速推 GET /api/v3/mcp/models（与探测同源），避免海外机/未探测时下拉无数据。
    """
    snap = read_sutui_llm_snapshot()
    models = list(snap.get("models") or []) if isinstance(snap.get("models"), list) else []
    probed_at = snap.get("probed_at")
    recommended = snap.get("recommended")
    error = snap.get("error")
    category_filter = snap.get("category_filter")
    live_fill = False

    if not models:
        try:
            token = await next_sutui_server_token(is_admin=True)
            if not token:
                raise RuntimeError("速推管理员 Token 池未配置，无法拉取 LLM 列表")
            raw = await _fetch_mcp_models(token)
            filtered = _filter_models_by_category(raw)
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
                if not recommended:
                    recommended = out[0]["id"]
                error = None
        except Exception as e:
            if not error:
                error = str(e)[:800]

    return {
        "probed_at": probed_at,
        "recommended": recommended,
        "models": models,
        "error": error,
        "category_filter": category_filter,
        "live_fill": live_fill,
    }
