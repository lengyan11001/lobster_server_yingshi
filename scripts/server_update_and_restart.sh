#!/usr/bin/env bash
# 在服务器上执行：拉取最新代码并重启 Backend + MCP
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "[更新] 拉取 origin main ..."
git fetch origin main
git pull origin main

if [ -x "$ROOT/.venv/bin/pip" ]; then
  echo "[依赖] .venv pip install -r requirements.txt ..."
  "$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"
else
  echo "[ERR] 未找到 $ROOT/.venv/bin/pip，请先在本机创建虚拟环境并安装依赖后再部署。"
  exit 1
fi

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files --type=service 2>/dev/null | grep -q lobster-backend; then
  echo "[重启] systemctl restart lobster-backend lobster-mcp ..."
  sudo systemctl restart lobster-backend lobster-mcp
  sudo systemctl status lobster-backend lobster-mcp --no-pager || true
  echo "[完成] 服务已重启"
else
  echo "[重启] 无 systemd，结束旧进程并后台启动 MCP + Backend ..."
  export PYTHONPATH="$ROOT"
  [ -f .env ] && set -a && . ./.env && set +a
  PY="$ROOT/.venv/bin/python"
  pkill -f "backend.run" 2>/dev/null || true
  pkill -f "mcp --port 8001" 2>/dev/null || true
  pkill -f "python -m mcp" 2>/dev/null || true
  sleep 2
  nohup "$PY" -m mcp --port "${MCP_PORT:-8001}" >> mcp.log 2>&1 &
  sleep 1
  nohup "$PY" -m backend.run >> backend.log 2>&1 &
  sleep 2
  echo "[完成] MCP 与 Backend 已后台启动，日志: mcp.log / backend.log"
fi
