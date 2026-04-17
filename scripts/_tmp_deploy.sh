#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOBSTER_DEPLOY_HOST=$(grep '^LOBSTER_DEPLOY_HOST=' "$ROOT/.env.deploy" | head -1 | cut -d= -f2-)
LOBSTER_DEPLOY_SSH_KEY=$(grep '^LOBSTER_DEPLOY_SSH_KEY=' "$ROOT/.env.deploy" | head -1 | cut -d= -f2-)
LOBSTER_DEPLOY_REMOTE_DIR=$(grep '^LOBSTER_DEPLOY_REMOTE_DIR=' "$ROOT/.env.deploy" | head -1 | cut -d= -f2-)
LOBSTER_SSH_KEY_PASSPHRASE=$(grep '^LOBSTER_SSH_KEY_PASSPHRASE=' "$ROOT/.env.deploy" | head -1 | cut -d= -f2-)
export LOBSTER_DEPLOY_HOST LOBSTER_DEPLOY_SSH_KEY LOBSTER_DEPLOY_REMOTE_DIR LOBSTER_SSH_KEY_PASSPHRASE

: "${LOBSTER_DEPLOY_HOST:?missing}"
REMOTE_DIR="${LOBSTER_DEPLOY_REMOTE_DIR:-/opt/lobster-server}"
SSH_BASE="-o StrictHostKeyChecking=accept-new"

_ssh_agent_has_keys() { [ -n "${SSH_AUTH_SOCK:-}" ] && ssh-add -l >/dev/null 2>&1; }
_DEPLOY_SSH_AGENT_STARTED=0
_deploy_cleanup_ssh_agent() { [ "$_DEPLOY_SSH_AGENT_STARTED" = 1 ] && { eval "$(ssh-agent -k)" 2>/dev/null || true; _DEPLOY_SSH_AGENT_STARTED=0; }; }
if ! _ssh_agent_has_keys; then
  if [ -n "${LOBSTER_DEPLOY_SSH_KEY:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -n "${LOBSTER_SSH_KEY_PASSPHRASE:-}" ]; then
    AP="$(mktemp)"; { echo '#!/usr/bin/env sh'; echo "printf '%s\\n' '$LOBSTER_SSH_KEY_PASSPHRASE'"; } > "$AP"; chmod +x "$AP"
    trap 'rm -f "$AP"; _deploy_cleanup_ssh_agent' EXIT
    eval "$(ssh-agent -s)"; _DEPLOY_SSH_AGENT_STARTED=1
    export SSH_ASKPASS_REQUIRE=force SSH_ASKPASS="$AP" DISPLAY="${DISPLAY:-localhost:0}"
    ssh-add "$LOBSTER_DEPLOY_SSH_KEY"; rm -f "$AP"; trap _deploy_cleanup_ssh_agent EXIT
  fi
fi
SSH_OPTS="$SSH_BASE"
! _ssh_agent_has_keys && [ -n "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && SSH_OPTS="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_BASE"

echo "=== 部署到 $LOBSTER_DEPLOY_HOST:$REMOTE_DIR ==="
ssh $SSH_OPTS "$LOBSTER_DEPLOY_HOST" bash -s <<REMOTE
cd "$REMOTE_DIR"
echo "--- git pull ---"
git pull origin main
echo "--- 重启服务 ---"
if [ -f scripts/server_update_and_restart.sh ]; then
  bash scripts/server_update_and_restart.sh
else
  sudo systemctl restart lobster-server 2>/dev/null || echo "手动重启"
fi
echo "=== 部署完成 ==="
REMOTE
