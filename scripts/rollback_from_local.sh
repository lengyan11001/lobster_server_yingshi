#!/usr/bin/env bash
# 在本地开发机执行：SSH 到服务器回滚到上次部署前的版本
# 用法：
#   bash scripts/rollback_from_local.sh          # 回滚到自动记录的版本
#   bash scripts/rollback_from_local.sh <hash>   # 回滚到指定 commit
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -f "$ROOT/.env.deploy" ]; then
  set -a
  . "$ROOT/.env.deploy"
  set +a
fi

if [ -z "$LOBSTER_DEPLOY_HOST" ]; then
  echo "[ERR] 未设置 LOBSTER_DEPLOY_HOST" >&2
  exit 1
fi

REMOTE_DIR="${LOBSTER_DEPLOY_REMOTE_DIR:-/opt/lobster-server}"
TARGET="${1:-}"

export GIT_TERMINAL_PROMPT=0
SSH_BASE="-o StrictHostKeyChecking=accept-new"

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

if ! _ssh_agent_has_keys; then
  if [ -n "${LOBSTER_DEPLOY_SSH_KEY:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -n "${LOBSTER_SSH_KEY_PASSPHRASE:-}" ]; then
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
  fi
fi

SSH_OPTS="$SSH_BASE"
if ! _ssh_agent_has_keys; then
  [ -n "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && SSH_OPTS="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_BASE"
fi

echo "[回滚] SSH $LOBSTER_DEPLOY_HOST → cd $REMOTE_DIR && bash scripts/rollback_server.sh $TARGET"
ssh $SSH_OPTS "$LOBSTER_DEPLOY_HOST" "cd $REMOTE_DIR && bash scripts/rollback_server.sh $TARGET"
echo "[完成] 服务器已回滚"
