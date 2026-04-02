#!/usr/bin/env python3
"""用 data/sutui_llm_catalog_from_api.json 覆盖 OpenClaw 里 lobster-sutui 的 models，并为每个 id 追加 agents.list 项。

用法:
  python scripts/export_sutui_llm_catalog.py
  python scripts/merge_openclaw_lobster_sutui_models.py \\
    --openclaw-json ../lobster_online_openclaw_lab/openclaw/openclaw.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List


def _slug(mid: str) -> str:
    s = re.sub(r"[^\w.-]+", "-", mid.strip(), flags=re.ASCII)
    s = re.sub(r"-+", "-", s).strip("-").lower()
    return s[:72] or "m"


def _to_openclaw_model(m: Dict[str, Any]) -> Dict[str, Any]:
    mid = str(m.get("id") or "").strip()
    name = str(m.get("name") or mid).strip() or mid
    caps = m.get("capabilities") or []
    has_vision = isinstance(caps, list) and "vision" in [str(x).lower() for x in caps]
    ctx = m.get("context_length")
    try:
        cw = int(ctx) if ctx is not None else 65536
    except (TypeError, ValueError):
        cw = 65536
    cw = max(4096, min(cw, 2_000_000))
    mid_l = mid.lower()
    reasoning = "reasoner" in mid_l or mid_l in ("o3", "o4-mini")
    out_max = min(16384, max(4096, cw // 16))
    return {
        "id": mid,
        "name": f"速推·{name}",
        "reasoning": reasoning,
        "input": ["text", "image"] if has_vision else ["text"],
        "contextWindow": cw,
        "maxTokens": out_max,
    }


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=str(root / "data" / "sutui_llm_catalog_from_api.json"))
    ap.add_argument("--openclaw-json", required=True)
    ap.add_argument("--primary-id", default="deepseek-chat")
    args = ap.parse_args()

    cat_path = Path(args.catalog)
    if not cat_path.is_file():
        print(f"[ERR] 缺少 {cat_path} ，请先运行 scripts/export_sutui_llm_catalog.py", file=sys.stderr)
        return 1
    oc_path = Path(args.openclaw_json)
    if not oc_path.is_file():
        print(f"[ERR] 找不到 {oc_path}", file=sys.stderr)
        return 2

    catalog = json.loads(cat_path.read_text(encoding="utf-8"))
    raw = catalog.get("models") or []
    openclaw_models = [_to_openclaw_model(m) for m in raw if isinstance(m, dict) and (m.get("id") or "").strip()]
    openclaw_models.sort(key=lambda x: x["id"].lower())

    primary_id = (args.primary_id or "").strip()
    ids = {x["id"] for x in openclaw_models}
    if primary_id not in ids:
        primary_id = openclaw_models[0]["id"] if openclaw_models else "deepseek-chat"

    cfg = json.loads(oc_path.read_text(encoding="utf-8"))
    prov = cfg.setdefault("models", {}).setdefault("providers", {}).setdefault("lobster-sutui", {})
    if not isinstance(prov, dict):
        print("[ERR] lobster-sutui 配置不是对象", file=sys.stderr)
        return 4
    prov["models"] = openclaw_models

    cfg.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})["primary"] = f"lobster-sutui/{primary_id}"

    old_list = cfg.setdefault("agents", {}).setdefault("list", [])
    if not isinstance(old_list, list):
        old_list = []
    main_entries = [a for a in old_list if isinstance(a, dict) and a.get("id") == "main"]
    main_entry = main_entries[0] if main_entries else {"id": "main", "default": True}
    # 去掉旧 lobster-sutui agent、去掉会与新 id 冲突的重复
    others = [
        a
        for a in old_list
        if isinstance(a, dict)
        and a.get("id") != "main"
        and not (isinstance(a.get("id"), str) and str(a["id"]).startswith("lobster-sutui-"))
        and not (isinstance(a.get("model"), str) and str(a["model"]).startswith("lobster-sutui/"))
    ]
    sutui_agents = []
    used_agent_ids: set[str] = set()
    for om in openclaw_models:
        mid = om["id"]
        aid = f"lobster-sutui-{_slug(mid)}"
        if aid in used_agent_ids:
            aid = f"lobster-sutui-{_slug(mid)}-x"
        used_agent_ids.add(aid)
        sutui_agents.append({"id": aid, "model": f"lobster-sutui/{mid}"})

    cfg["agents"]["list"] = [main_entry] + sutui_agents + others

    oc_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[OK] {oc_path}")
    print(f"     lobster-sutui.models={len(openclaw_models)} primary=lobster-sutui/{primary_id}")
    print(f"     agents.list={len(cfg['agents']['list'])} (含 main + {len(sutui_agents)} 个速推)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
