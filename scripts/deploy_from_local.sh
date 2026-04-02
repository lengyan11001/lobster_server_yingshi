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
SSH_BASE="-o StrictHostKeyChecking=accept-new"
# 若本机已 ssh-add 解锁密钥，不要用 -i（否则会再次读盘加密私钥、非交互易失败）
_ssh_agent_has_keys() {
  [ -n "${SSH_AUTH_SOCK:-}" ] && ssh-add -l >/dev/null 2>&1
}
SSH_OPTS_MAIN="$SSH_BASE"
if ! _ssh_agent_has_keys; then
  [ -n "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && SSH_OPTS_MAIN="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_BASE"
fi
# 海外机若未授权大陆同一把 key，可单独配 LOBSTER_DEPLOY_SSH_KEY_OVERSEAS
SSH_OPTS_OS="$SSH_BASE"
if ! _ssh_agent_has_keys; then
  if [ -n "$LOBSTER_DEPLOY_SSH_KEY_OVERSEAS" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY_OVERSEAS" ]; then
    SSH_OPTS_OS="-i $LOBSTER_DEPLOY_SSH_KEY_OVERSEAS $SSH_BASE"
  elif [ -n "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ]; then
    SSH_OPTS_OS="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_BASE"
  fi
fi

_run_remote() {
  local host="$1"
  local dir="$2"
  local sshopts="$3"
  echo "[部署] SSH $host → cd $dir && git pull origin main && bash scripts/server_update_and_restart.sh"
  ssh $sshopts "$host" "cd $dir && git fetch origin main && git pull origin main && bash scripts/server_update_and_restart.sh"
}

_run_remote "$LOBSTER_DEPLOY_HOST" "$REMOTE_DIR" "$SSH_OPTS_MAIN"
echo "[完成] 大陆/主服务器已更新并重启"

if [ -n "$LOBSTER_DEPLOY_HOST_OVERSEAS" ]; then
  _run_remote "$LOBSTER_DEPLOY_HOST_OVERSEAS" "$REMOTE_DIR_OS" "$SSH_OPTS_OS"
  echo "[完成] 海外服务器已更新并重启"
fi
