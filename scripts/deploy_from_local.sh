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

# 避免 Git/部分工具弹 TTY 索要口令；SSH 加密私钥须配 LOBSTER_SSH_KEY_PASSPHRASE 走下面 SSH_ASKPASS
export GIT_TERMINAL_PROMPT=0

if [ -z "$LOBSTER_DEPLOY_HOST" ]; then
  echo "未设置 LOBSTER_DEPLOY_HOST，无法远程执行。"
  echo "可创建 .env.deploy 写入 LOBSTER_DEPLOY_HOST=user@IP、LOBSTER_DEPLOY_SSH_KEY=密钥路径、LOBSTER_DEPLOY_REMOTE_DIR=服务器目录"
  echo "可选：LOBSTER_DEPLOY_HOST_OVERSEAS=root@海外IP（与 lobster-server.icu 同机时填其解析地址）"
  echo "或在服务器上执行: cd /opt/lobster-server && bash scripts/server_update_and_restart.sh"
  exit 1
fi

REMOTE_DIR="${LOBSTER_DEPLOY_REMOTE_DIR:-/opt/lobster-server}"
REMOTE_DIR_OS="${LOBSTER_DEPLOY_REMOTE_DIR_OVERSEAS:-$REMOTE_DIR}"
SSH_BASE="-o StrictHostKeyChecking=accept-new"
# 若本机已 ssh-add 解锁密钥，不要用 -i（否则会再次读盘加密私钥、易弹窗索要口令）
_ssh_agent_has_keys() {
  [ -n "${SSH_AUTH_SOCK:-}" ] && ssh-add -l >/dev/null 2>&1
}

_DEPLOY_SSH_AGENT_STARTED=0
_deploy_cleanup_ssh_agent() {
  if [ "$_DEPLOY_SSH_AGENT_STARTED" = 1 ]; then
    eval "$(ssh-agent -k)" 2>/dev/null || true
    _DEPLOY_SSH_AGENT_STARTED=0
  fi
}

# 加密私钥但未配口令、且 agent 里无密钥时，禁止继续（否则会交互式索要密码）
_ssh_private_key_seems_encrypted() {
  local k="$1"
  [ ! -r "$k" ] && return 1
  grep -q "ENCRYPTED" "$k" 2>/dev/null && return 0
  if ssh-keygen -y -f "$k" -P "" >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

if ! _ssh_agent_has_keys; then
  if [ -n "${LOBSTER_DEPLOY_SSH_KEY:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ]; then
    if _ssh_private_key_seems_encrypted "$LOBSTER_DEPLOY_SSH_KEY" && [ -z "${LOBSTER_SSH_KEY_PASSPHRASE:-}" ]; then
      echo "[ERR] 部署私钥已加密：请在 lobster-server/.env.deploy 配置 LOBSTER_SSH_KEY_PASSPHRASE（脚本用 SSH_ASKPASS 非交互解锁），或在本机先 ssh-add 再部署。不要依赖终端弹窗输入。" >&2
      exit 1
    fi
  fi
fi

# 加密私钥：若 .env.deploy 中有 LOBSTER_SSH_KEY_PASSPHRASE，用 SSH_ASKPASS 非交互 ssh-add，避免 GUI/终端弹窗
if ! _ssh_agent_has_keys; then
  if [ -n "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -n "${LOBSTER_SSH_KEY_PASSPHRASE:-}" ]; then
    AP="$(mktemp)"
    {
      echo '#!/usr/bin/env sh'
      echo 'printf %s\\n "$LOBSTER_SSH_KEY_PASSPHRASE"'
    } > "$AP"
    chmod +x "$AP"
    trap 'rm -f "$AP"; _deploy_cleanup_ssh_agent' EXIT
    eval "$(ssh-agent -s)"
    _DEPLOY_SSH_AGENT_STARTED=1
    export SSH_ASKPASS_REQUIRE=force
    export SSH_ASKPASS="$AP"
    export DISPLAY="${DISPLAY:-localhost:0}"
    ssh-add "$LOBSTER_DEPLOY_SSH_KEY"
    rm -f "$AP"
    trap _deploy_cleanup_ssh_agent EXIT
    if [ -n "$LOBSTER_DEPLOY_HOST_OVERSEAS" ] && [ -n "${LOBSTER_DEPLOY_SSH_KEY_OVERSEAS:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY_OVERSEAS" ]; then
      ssh-add "$LOBSTER_DEPLOY_SSH_KEY_OVERSEAS" || true
    fi
  fi
fi

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
