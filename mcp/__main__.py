"""MCP Server entry: run HTTP server on configured port.
全日志：默认 LOG_LEVEL=debug；.env 或环境变量 LOG_LEVEL=info 可仅打关键信息。"""
import logging
import os
import sys
from pathlib import Path

import uvicorn
from . import http_server

if __name__ == "__main__":
    _root = Path(__file__).resolve().parent.parent
    try:
        from dotenv import load_dotenv

        load_dotenv(_root / ".env")
    except ImportError:
        pass
    port = 8001
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            try:
                port = int(sys.argv[i + 1])
            except ValueError:
                pass
            break
    log_level_name = os.environ.get("LOG_LEVEL", "debug").strip().lower()
    log_level = getattr(logging, log_level_name.upper(), logging.DEBUG)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("mcp")
    logger.info("[启动] MCP 服务监听 127.0.0.1:%s LOG_LEVEL=%s", port, log_level_name)
    uvicorn.run(
        http_server.app,
        host="127.0.0.1",
        port=port,
        log_level=log_level_name,
    )
