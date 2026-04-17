#!/usr/bin/env bash
# 在服务器上执行：拉取最新代码并重启 Backend + MCP
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PREV_COMMIT="$(git rev-parse HEAD)"
echo "$PREV_COMMIT" > "$ROOT/.deploy_rollback_commit"
echo "[备份] 当前版本 $PREV_COMMIT 已记录到 .deploy_rollback_commit"

echo "[更新] 拉取 origin main ..."
git fetch origin main
git pull origin main

NEW_COMMIT="$(git rev-parse HEAD)"
echo "[版本] $PREV_COMMIT → $NEW_COMMIT"

if [ -x "$ROOT/.venv/bin/pip" ]; then
  echo "[依赖] .venv pip install -r requirements.txt ..."
  "$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"
else
  echo "[ERR] 未找到 $ROOT/.venv/bin/pip，请先在本机创建虚拟环境并安装依赖后再部署。"
  exit 1
fi

echo "[日志] 截断 $ROOT/mcp.log / backend.log（若存在；便于本轮只看新输出如 [sutui-audit]）"
: > "$ROOT/mcp.log" 2>/dev/null || true
: > "$ROOT/backend.log" 2>/dev/null || true

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files --type=service 2>/dev/null | grep -q lobster-backend; then
  echo "[重启] systemctl stop + 端口清理 + start ..."
  sudo systemctl stop lobster-mcp lobster-backend 2>/dev/null || true
  sleep 1
  # 确保 8001/8000 端口无残留进程
  for PORT in 8001 8000; do
    PID_ON_PORT="$(sudo fuser "$PORT/tcp" 2>/dev/null | tr -d '[:space:]')" || true
    if [ -n "$PID_ON_PORT" ]; then
      echo "[清理] 端口 $PORT 仍被进程 $PID_ON_PORT 占用，强制结束"
      sudo fuser -k "$PORT/tcp" 2>/dev/null || true
      sleep 1
    fi
  done
  sudo systemctl start lobster-backend lobster-mcp
  sleep 2
  # 验证 MCP 是否成功监听
  MCP_OK=0
  for i in 1 2 3; do
    if sudo fuser 8001/tcp >/dev/null 2>&1; then
      MCP_OK=1; break
    fi
    echo "[等待] MCP 端口 8001 尚未就绪，等待 ${i}s..."
    sleep "$i"
  done
  if [ "$MCP_OK" = 0 ]; then
    echo "[WARN] MCP 可能未成功启动，检查 mcp.log"
  fi
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
