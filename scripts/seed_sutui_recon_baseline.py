#!/usr/bin/env python3
"""在项目根执行：拉取必火/影视 server token 的速推余额并写入对账基线（DB + data/sutui_reconcile_baseline.json）。

用法：
  cd /path/to/lobster_server
  export PYTHONPATH=.   # Windows: set PYTHONPATH=.
  .venv/bin/python scripts/seed_sutui_recon_baseline.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> None:
    import os

    os.chdir(ROOT)
    if not (ROOT / "backend").is_dir():
        print("请在 lobster_server 项目根目录运行。", file=sys.stderr)
        sys.exit(2)
    from backend.app.services.sutui_reconcile import seed_sutui_reconciliation_baseline

    out = seed_sutui_reconciliation_baseline()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
