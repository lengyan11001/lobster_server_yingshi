#!/usr/bin/env bash
# 本机执行：server 走 git 拉取并重启 + 将 lobster_online/static 同步到服务器（与 deploy_from_local 同一 SSH）
# 依赖 lobster-server/.env.deploy：LOBSTER_DEPLOY_HOST、LOBSTER_ONLINE_REMOTE_DIR；可选 LOBSTER_DEPLOY_SSH_KEY、LOBSTER_DEPLOY_REMOTE_DIR、LOBSTER_ONLINE_LOCAL_DIR
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
  echo "[ERR] 未设置 LOBSTER_DEPLOY_HOST，无法部署。请在 .env.deploy 中配置。"
  exit 1
fi
if [ -z "$LOBSTER_ONLINE_REMOTE_DIR" ]; then
  echo "[ERR] 完整部署需设置 LOBSTER_ONLINE_REMOTE_DIR（服务器上 lobster_online 根目录，如 /root/lobster_online）。"
  exit 1
fi

ONLINE_SRC="${LOBSTER_ONLINE_LOCAL_DIR:-$(cd "$ROOT/.." && pwd)/lobster_online}"
if [ ! -d "$ONLINE_SRC/static" ]; then
  echo "[ERR] 未找到本地 online 静态目录: $ONLINE_SRC/static"
  exit 1
fi

SSH_OPTS="-o StrictHostKeyChecking=accept-new"
[ -n "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && SSH_OPTS="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_OPTS"

echo "[1/2] server: git pull + restart"
bash "$ROOT/scripts/deploy_from_local.sh"

echo "[2/2] online: rsync static → $LOBSTER_DEPLOY_HOST:$LOBSTER_ONLINE_REMOTE_DIR/static/"
rsync -avz --delete -e "ssh $SSH_OPTS" \
  "$ONLINE_SRC/static/" \
  "$LOBSTER_DEPLOY_HOST:$LOBSTER_ONLINE_REMOTE_DIR/static/"

echo "[完成] server 已更新并重启；lobster_online/static 已同步"
