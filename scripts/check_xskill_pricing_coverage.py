#!/usr/bin/env python3
"""对照速推公开 API：列出无 pricing 的模型（需联网）。用法: python scripts/check_xskill_pricing_coverage.py [--base URL]"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List
from urllib.parse import quote

import httpx


def fetch_models(base: str, lang: str = "zh") -> List[Dict[str, Any]]:
    r = httpx.get(f"{base}/api/v3/mcp/models", params={"lang": lang}, timeout=30.0)
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, dict) or int(j.get("code", 0)) != 200:
        raise RuntimeError(f"models 返回异常: {j!r}")
    data = j.get("data") or {}
    models = data.get("models") or []
    if not isinstance(models, list):
        raise RuntimeError("models 无列表")
    return models


def fetch_pricing(base: str, model_id: str, lang: str = "zh") -> Any:
    safe = quote(model_id, safe="")
    r = httpx.get(f"{base}/api/v3/models/{safe}/docs", params={"lang": lang}, timeout=30.0)
    r.raise_for_status()
    j = r.json()
    if not isinstance(j, dict) or int(j.get("code", 0)) != 200:
        return None
    data = j.get("data")
    if not isinstance(data, dict):
        return None
    return data.get("pricing")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://api.xskill.ai", help="xskill API 根地址")
    ap.add_argument("--delay", type=float, default=0.2, help="每模型请求间隔秒")
    ap.add_argument("--limit", type=int, default=0, help="仅检查前 N 个模型（0=全部）")
    ap.add_argument("--out", default="", help="可选：输出 JSON 报告路径")
    args = ap.parse_args()
    base = str(args.base).rstrip("/")

    models = fetch_models(base)
    if args.limit and args.limit > 0:
        models = models[: int(args.limit)]
    total = len(models)
    missing: List[Dict[str, Any]] = []
    ok: List[str] = []

    for i, m in enumerate(models):
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        print(f"[{i + 1}/{total}] {mid} ... ", end="", flush=True)
        try:
            p = fetch_pricing(base, mid)
            if p is None:
                print("无 pricing")
                missing.append({"id": mid, "name": m.get("name"), "category": m.get("category")})
            else:
                print("OK")
                ok.append(mid)
        except Exception as e:
            print(f"失败: {e}")
            missing.append({"id": mid, "name": m.get("name"), "error": str(e)})
        time.sleep(max(0.0, float(args.delay)))

    print()
    print("=" * 72)
    print(f"模型总数: {total}")
    print(f"有 pricing: {len(ok)}")
    print(f"无 pricing 或失败: {len(missing)}")
    if missing:
        print("\n无 pricing 或拉取失败（前 50 条）:")
        for row in missing[:50]:
            print(json.dumps(row, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"total": total, "with_pricing": ok, "missing": missing}, f, ensure_ascii=False, indent=2)
        print(f"\n已写入 {args.out}")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
