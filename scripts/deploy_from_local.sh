#!/usr/bin/env bash
# 在本地开发机执行：推送后通过 SSH 在服务器上拉取并重启（需配置 LOBSTER_DEPLOY_HOST）
# 可选：LOBSTER_DEPLOY_HOST_OVERSEAS → lobster-server.icu 等海外机（Messenger/Twilio 等需出海 API）
# 若存在 .env.deploy 会自动加载（勿提交，已 gitignore）
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -f "$ROOT/.env.deploy" ]; then
  set -a
  # shellcheck source=../.env.deploy
  . "$ROOT/.env.deploy"
  set +a
fi

if [ -z "$LOBSTER_DEPLOY_HOST" ]; then
  echo "未设置 LOBSTER_DEPLOY_HOST，无法远程执行。"
  echo "可创建 .env.deploy 写入 LOBSTER_DEPLOY_HOST=user@IP、LOBSTER_DEPLOY_SSH_KEY=密钥路径、LOBSTER_DEPLOY_REMOTE_DIR=服务器目录"
  echo "可选：LOBSTER_DEPLOY_HOST_OVERSEAS=root@海外IP（与 lobster-server.icu 同机时填其解析地址）"
  echo "或在服务器上执行: cd /root/lobster_server && bash scripts/server_update_and_restart.sh"
  exit 1
fi

REMOTE_DIR="${LOBSTER_DEPLOY_REMOTE_DIR:-/opt/lobster-server}"
REMOTE_DIR_OS="${LOBSTER_DEPLOY_REMOTE_DIR_OVERSEAS:-$REMOTE_DIR}"
SSH_OPTS="-o StrictHostKeyChecking=accept-new"
[ -n "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && SSH_OPTS="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_OPTS"

_run_remote() {
  local host="$1"
  local dir="$2"
  echo "[部署] SSH $host → cd $dir && git pull origin main && bash scripts/server_update_and_restart.sh"
  ssh $SSH_OPTS "$host" "cd $dir && git fetch origin main && git pull origin main && bash scripts/server_update_and_restart.sh"
}

_run_remote "$LOBSTER_DEPLOY_HOST" "$REMOTE_DIR"
echo "[完成] 大陆/主服务器已更新并重启"

if [ -n "$LOBSTER_DEPLOY_HOST_OVERSEAS" ]; then
  _run_remote "$LOBSTER_DEPLOY_HOST_OVERSEAS" "$REMOTE_DIR_OS"
  echo "[完成] 海外服务器已更新并重启"
fi
