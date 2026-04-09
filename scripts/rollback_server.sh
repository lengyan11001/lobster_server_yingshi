#!/usr/bin/env bash
# 服务器上执行：回滚到上次部署前的版本并重启
# 用法：
#   bash scripts/rollback_server.sh          # 回滚到 .deploy_rollback_commit 记录的版本
#   bash scripts/rollback_server.sh <hash>   # 回滚到指定 commit
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  if [ -f "$ROOT/.deploy_rollback_commit" ]; then
    TARGET="$(cat "$ROOT/.deploy_rollback_commit")"
    echo "[回滚] 从 .deploy_rollback_commit 读取目标: $TARGET"
  else
    echo "[ERR] 无 .deploy_rollback_commit 文件且未指定 commit hash。"
    echo "用法: bash scripts/rollback_server.sh <commit_hash>"
    echo "查看历史: git log --oneline -10"
    exit 1
  fi
fi

CURRENT="$(git rev-parse HEAD)"
echo "[当前] $CURRENT"
echo "[目标] $TARGET"

if [ "$CURRENT" = "$TARGET" ]; then
  echo "[跳过] 当前已在目标版本，仅重启服务。"
else
  git checkout "$TARGET"
  echo "[回滚] 已切换到 $TARGET"
fi

if [ -x "$ROOT/.venv/bin/pip" ]; then
  echo "[依赖] pip install -r requirements.txt ..."
  "$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"
fi

echo "[日志] 截断日志"
: > "$ROOT/mcp.log" 2>/dev/null || true
: > "$ROOT/backend.log" 2>/dev/null || true

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files --type=service 2>/dev/null | grep -q lobster-backend; then
  echo "[重启] systemctl restart lobster-backend lobster-mcp ..."
  sudo systemctl restart lobster-backend lobster-mcp
  sudo systemctl status lobster-backend lobster-mcp --no-pager || true
else
  echo "[重启] 后台重启 MCP + Backend ..."
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
fi

echo "[完成] 已回滚到 $TARGET 并重启服务"
