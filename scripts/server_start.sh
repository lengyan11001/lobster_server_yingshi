#!/usr/bin/env bash
# 云服务器：启动 MCP + Backend（拉取代码后快速启动）
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"

# 若未安装则先安装
if [ ! -d ".venv" ] || [ ! -x ".venv/bin/python" ]; then
  echo "未检测到 .venv，先执行安装：./scripts/server_install.sh"
  exit 1
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "已复制 .env.example → .env，请编辑 .env 填写 SECRET_KEY、SUTUI_SERVER_TOKEN 后重新运行本脚本"
  exit 1
fi

PY="$ROOT/.venv/bin/python"
PORT="${PORT:-8000}"
MCP_PORT="${MCP_PORT:-8001}"

# 若 8001 已在监听则跳过，否则后台启动 MCP
start_mcp() {
  if "$PY" -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1)
try:
  s.connect(('127.0.0.1', $MCP_PORT))
  s.close()
  exit(0)
except Exception:
  exit(1)
" 2>/dev/null; then
    echo "[MCP] 端口 $MCP_PORT 已在监听，跳过"
    return
  fi
  echo "[MCP] 启动 MCP 端口 $MCP_PORT ..."
  nohup "$PY" -m mcp --port "$MCP_PORT" >> mcp.log 2>&1 &
  sleep 1
}

start_mcp

echo "[Backend] 启动 Backend 端口 $PORT ..."
echo "访问: http://0.0.0.0:$PORT"
exec "$PY" -m backend.run
