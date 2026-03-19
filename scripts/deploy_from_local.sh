#!/usr/bin/env bash
# 在本地开发机执行：推送后通过 SSH 在服务器上拉取并重启（需配置 LOBSTER_DEPLOY_HOST）
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
  echo "或在服务器上执行: cd /root/lobster_server && bash scripts/server_update_and_restart.sh"
  exit 1
fi

REMOTE_DIR="${LOBSTER_DEPLOY_REMOTE_DIR:-/opt/lobster-server}"
SSH_OPTS="-o StrictHostKeyChecking=accept-new"
[ -n "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && SSH_OPTS="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_OPTS"
echo "[部署] SSH $LOBSTER_DEPLOY_HOST → cd $REMOTE_DIR && git pull origin main && bash scripts/server_update_and_restart.sh"
ssh $SSH_OPTS "$LOBSTER_DEPLOY_HOST" "cd $REMOTE_DIR && git fetch origin main && git pull origin main && bash scripts/server_update_and_restart.sh"
echo "[完成] 服务器已更新并重启"
