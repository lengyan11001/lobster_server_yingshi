#!/usr/bin/env python3
"""
将旧版双池写入品牌池：
  SUTUI_SERVER_TOKENS_ADMIN（或 TOKEN_ADMIN） → SUTUI_SERVER_TOKENS_YINGSHI
  SUTUI_SERVER_TOKENS_USER（或 TOKEN_USER）   → SUTUI_SERVER_TOKENS_BIHUO
并注释上述 ADMIN/USER 键；注释裸 SUTUI_SERVER_TOKEN，避免与品牌池并存混淆。
在项目根目录执行：python3 scripts/migrate_sutui_brand_pools_env.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from shutil import copy2
from time import time


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if not env_path.is_file():
        print("ERR: .env 不存在", file=sys.stderr)
        return 1
    raw_lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)

    def get_val(key: str) -> str:
        for line in raw_lines:
            t = line.strip()
            if not t or t.startswith("#"):
                continue
            m = re.match(rf"^{re.escape(key)}=(.*)$", t)
            if m:
                return m.group(1).strip().strip('"').strip("'")
        return ""

    admin = get_val("SUTUI_SERVER_TOKENS_ADMIN") or get_val("SUTUI_SERVER_TOKEN_ADMIN")
    user = get_val("SUTUI_SERVER_TOKENS_USER") or get_val("SUTUI_SERVER_TOKEN_USER")
    if not admin or not user:
        print(
            "ERR: 需在 .env 中同时存在 SUTUI_SERVER_TOKENS_ADMIN（或 TOKEN_ADMIN）"
            " 与 SUTUI_SERVER_TOKENS_USER（或 TOKEN_USER）",
            file=sys.stderr,
        )
        return 1

    bak = env_path.with_name(f".env.bak.migrate_brand.{int(time())}")
    copy2(env_path, bak)

    drop_keys = {
        "SUTUI_SERVER_TOKENS_BIHUO",
        "SUTUI_SERVER_TOKEN_BIHUO",
        "SUTUI_SERVER_TOKENS_YINGSHI",
        "SUTUI_SERVER_TOKEN_YINGSHI",
    }
    # 注释旧 ADMIN/USER 单行；USER 将在文末用与 BIHUO 相同的值重建，供无品牌用户兜底
    comment_keys = {
        "SUTUI_SERVER_TOKENS_ADMIN",
        "SUTUI_SERVER_TOKEN_ADMIN",
        "SUTUI_SERVER_TOKENS_USER",
        "SUTUI_SERVER_TOKEN_USER",
        "SUTUI_SERVER_TOKEN",
        "SUTUI_SERVER_TOKENS",
    }

    out: list[str] = []
    for line in raw_lines:
        t = line.strip()
        if not t or t.startswith("#"):
            out.append(line)
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", t)
        if not m:
            out.append(line)
            continue
        key = m.group(1)
        if key in drop_keys:
            out.append(f"# removed by migrate_sutui_brand_pools_env (replaced below): {t}\n")
            continue
        if key in comment_keys:
            out.append(f"# migrated to BIHUO/YINGSHI brand pools: {t}\n")
            continue
        out.append(line)

    out.append("\n# --- brand pools (原 ADMIN→yingshi, 原 USER→bihuo；USER 与 BIHUO 同值供无 brand_mark 兜底) ---\n")
    out.append(f"SUTUI_SERVER_TOKENS_YINGSHI={admin}\n")
    out.append(f"SUTUI_SERVER_TOKENS_BIHUO={user}\n")
    out.append(f"SUTUI_SERVER_TOKENS_USER={user}\n")

    env_path.write_text("".join(out), encoding="utf-8")
    print(f"OK: 已写入品牌池并备份至 {bak.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
