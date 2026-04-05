#!/usr/bin/env python3
"""打屏：对速推发一次请求，打印「用的哪把 token」和「对方返回什么」。

在项目根目录执行（与线上一致的环境变量）：
  set -a && . ./.env && set +a && python scripts/print_sutui_exchange.py
  python scripts/print_sutui_exchange.py --token sk-...
  python scripts/print_sutui_exchange.py --chat --model qwen-plus
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from mcp.sutui_tokens import _internal_probe_pool_and_list  # noqa: E402


def _api_base() -> str:
    return (os.environ.get("SUTUI_API_BASE") or "https://api.xskill.ai").rstrip("/")


def _pick_token(cli_token: Optional[str]) -> Tuple[str, str]:
    if (cli_token or "").strip():
        return cli_token.strip(), "cli"
    pool, lst = _internal_probe_pool_and_list()
    if not lst:
        print("ERROR: 无 token。请配置 SUTUI_SERVER_TOKENS_* / SUTUI_SERVER_TOKEN_* 或使用 --token", file=sys.stderr)
        sys.exit(2)
    return lst[0].strip(), pool


def main() -> None:
    ap = argparse.ArgumentParser(description="打印速推请求 token + 响应正文")
    ap.add_argument("--token", default=None, help="明文 sk；不设则从环境探测池取第一个")
    ap.add_argument("--chat", action="store_true", help="POST /v1/chat/completions；默认 GET /api/v3/balance")
    ap.add_argument("--model", default="qwen-plus", help="--chat 时的 model")
    args = ap.parse_args()

    token, pool = _pick_token(args.token)
    base = _api_base()
    ref = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]

    print("========== 速推请求 ==========")
    print(f"pool={pool}")
    print(f"token_ref={ref}")
    print(f"Authorization: Bearer {token}")
    print()

    if args.chat:
        url = f"{base}/v1/chat/completions"
        print(f"POST {url}")
        body = {
            "model": args.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
        with httpx.Client(timeout=60.0, trust_env=True) as client:
            r = client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
    else:
        url = f"{base}/api/v3/balance"
        print(f"GET {url}（若 4xx 会再试 api_key 查询参数）")
        with httpx.Client(timeout=30.0, trust_env=True) as client:
            r = client.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if r.status_code >= 400:
                r = client.get(url, params={"api_key": token}, headers={"Accept": "application/json"})

    print()
    print("========== 速推响应 ==========")
    print(f"HTTP {r.status_code}")
    try:
        obj = r.json()
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    except Exception:
        print((r.text or "")[:500_000])


if __name__ == "__main__":
    main()
