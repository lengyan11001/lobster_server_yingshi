"""启动后端：根据 LOBSTER_EDITION 决定监听地址；默认同时拉起 MCP(8001) 以提供速推等能力。
全日志：默认 LOG_LEVEL=debug；要减少输出可设 .env 中 LOG_LEVEL=info。
系统日志写入 lobster/logs/app.log，可在「日志」Tab 或 GET /api/logs 查看。"""
import logging
import os
import subprocess
import sys
import time

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)  # 使 .env 在 lobster 根目录被 pydantic 找到

# 全日志：由 LOG_LEVEL 控制，默认 debug（.env 可设 LOG_LEVEL=info 仅关键信息）
_log_level_name = os.environ.get("LOG_LEVEL", "debug").strip().lower()
_log_level = getattr(logging, _log_level_name.upper(), logging.DEBUG)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# 系统日志同时写入 lobster/logs/app.log，便于「日志」Tab 与排错
_log_dir = os.path.join(_root, "logs")
try:
    os.makedirs(_log_dir, exist_ok=True)
    _log_file = os.path.join(_log_dir, "app.log")
    _file_handler = logging.FileHandler(_log_file, mode="a", encoding="utf-8")
    _file_handler.setLevel(_log_level)
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(_file_handler)
except Exception as _e:
    pass  # 无写权限等则仅控制台输出
_logger = logging.getLogger("backend.run")

import uvicorn
from backend.app.core.config import settings


def _start_mcp_if_needed():
    """若 8001 未被占用则启动 MCP，使对话侧速推/能力可用。"""
    mcp_port = int(getattr(settings, "mcp_port", 8001))
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", mcp_port))
        s.close()
        _logger.info("[启动] MCP 已在端口 %s 监听，跳过自启", mcp_port)
        return
    except (OSError, socket.error):
        pass
    try:
        # 确保子进程能找到 mcp 包：优先用含 mcp 的目录作 PYTHONPATH（支持 lobster\lobster 嵌套时 mcp 在上一级）
        mcp_root = _root
        if not os.path.isdir(os.path.join(_root, "mcp")):
            _parent = os.path.dirname(_root)
            if os.path.isdir(os.path.join(_parent, "mcp")):
                mcp_root = _parent
                _logger.info("[启动] MCP 自启：mcp 在上一级 %s", mcp_root)
        mcp_log_path = os.path.join(mcp_root, "mcp.log")
        try:
            _mcp_log = open(mcp_log_path, "a", encoding="utf-8")
        except Exception:
            _mcp_log = subprocess.DEVNULL
        _cmd = (
            "import sys; sys.path.insert(0, %s); sys.argv = ['mcp', '--port', '%s']; "
            "import runpy; runpy.run_module('mcp', run_name='__main__', alter_sys=True)"
        ) % (repr(mcp_root), mcp_port)
        subprocess.Popen(
            [sys.executable, "-c", _cmd],
            cwd=mcp_root,
            env=os.environ.copy(),
            stdout=_mcp_log,
            stderr=subprocess.STDOUT if _mcp_log != subprocess.DEVNULL else subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.8)
        _logger.info("[启动] MCP 已自启，端口 %s (日志 %s)", mcp_port, mcp_log_path)
    except Exception as e:
        _logger.warning("[启动] MCP 自启失败: %s", e)


if __name__ == "__main__":
    _start_mcp_if_needed()
    port = int(os.environ.get("PORT", str(settings.port)))
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    # 监听地址：与 edition 无关，统一用配置（.env 或环境变量 HOST）。start.bat 默认 HOST=0.0.0.0 以便局域网访问
    host = (getattr(settings, "host", None) or os.environ.get("HOST") or "0.0.0.0").strip() or "0.0.0.0"
    _logger.info("[启动] Backend 启动 host=%s port=%s edition=%s LOG_LEVEL=%s", host, port, edition, _log_level_name)
    uvicorn.run(
        "backend.app.main:app",
        host=host,
        port=port,
        log_level=_log_level_name,
    )
