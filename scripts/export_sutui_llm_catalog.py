#!/usr/bin/env python3
"""从速推公开接口导出「对话 LLM」清单（id + mcp/models 返回的全部字段），不写死、不猜。

- 列表源: GET {base}/api/v3/mcp/models?lang=zh|en（与 sutui_llm_probe、model-pricing-guide 一致）
- 筛选: category 为 llm 或 text，且 task_type 为 chat（与网页速推 LLM 下拉一致）
- 说明: chat 模型在 xskill 上通常无 /api/v3/models/{id}/docs（会 404），OpenAI 兼容体用标准
  messages/model/stream/tools 等字段，无 invoke_capability 式 params_schema。

用法:
  python scripts/export_sutui_llm_catalog.py
  python scripts/export_sutui_llm_catalog.py --base https://api.xskill.ai --lang zh --out data/sutui_llm_catalog_from_api.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import httpx


def _is_chat_llm(m: Dict[str, Any]) -> bool:
    cat = str(m.get("category") or "").strip().lower()
    if cat not in ("llm", "text"):
        return False
    tt = str(m.get("task_type") or "").strip().lower()
    return tt == "chat"


def fetch_models(base: str, lang: str) -> List[Dict[str, Any]]:
    r = httpx.get(
        f"{base.rstrip('/')}/api/v3/mcp/models",
        params={"lang": lang},
        timeout=60.0,
        trust_env=False,
    )
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, dict) or int(j.get("code", 0)) != 200:
        raise RuntimeError(f"mcp/models 异常: {j!r}")
    data = j.get("data") or {}
    models = data.get("models") or []
    if not isinstance(models, list):
        raise RuntimeError("data.models 不是数组")
    return models


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://api.xskill.ai", help="xskill 根地址")
    ap.add_argument("--lang", default="zh", choices=("zh", "en"), help="mcp/models lang")
    ap.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "data" / "sutui_llm_catalog_from_api.json"),
        help="输出 JSON 路径",
    )
    args = ap.parse_args()
    base = str(args.base).rstrip("/")

    models = fetch_models(base, args.lang)
    chat_llms = [dict(m) for m in models if isinstance(m, dict) and _is_chat_llm(m)]
    chat_llms.sort(key=lambda x: str(x.get("id") or ""))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "exported_by": "scripts/export_sutui_llm_catalog.py",
        "source": f"GET {base}/api/v3/mcp/models?lang={args.lang}",
        "filter": "category in (llm, text) AND task_type == chat",
        "total_mcp_models": len(models),
        "chat_llm_count": len(chat_llms),
        "models": chat_llms,
        "note": (
            "chat LLM 的 chat/completions 使用 OpenAI 兼容 JSON；mcp/models 条目中的 id 即 POST body 的 model。"
            "若上游报 distributor/channel 错误，与目录 id 无关，为账户分销商路由配置问题。"
        ),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} ({len(chat_llms)} chat LLMs)")
    for m in chat_llms:
        print(f"  {m.get('id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
