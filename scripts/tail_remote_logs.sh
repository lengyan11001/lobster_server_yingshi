#!/usr/bin/env bash
# 读本机 .env.deploy，非交互 SSH（与 deploy_from_local.sh 同款 SSH_ASKPASS）上机 tail 日志。
# 密钥口令只放在 .env.deploy 的 LOBSTER_SSH_KEY_PASSPHRASE，勿提交该文件。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -f "$ROOT/.env.deploy" ]; then
  set -a
  # shellcheck source=../.env.deploy
  . "$ROOT/.env.deploy"
  set +a
fi
: "${LOBSTER_DEPLOY_HOST:?missing LOBSTER_DEPLOY_HOST in .env.deploy}"

REMOTE_DIR="${LOBSTER_DEPLOY_REMOTE_DIR:-/opt/lobster-server}"
TAIL_LINES="${TAIL_LINES:-200}"

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

SSH_OPTS_MAIN="$SSH_BASE"
if ! _ssh_agent_has_keys; then
  [ -n "${LOBSTER_DEPLOY_SSH_KEY:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && SSH_OPTS_MAIN="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_BASE"
fi

ssh $SSH_OPTS_MAIN "$LOBSTER_DEPLOY_HOST" bash -s <<EOF
cd $(printf %q "$REMOTE_DIR")
echo "==== host \$(hostname) pwd \$(pwd) ===="
for f in backend.log mcp.log; do
  if [ -f "\$f" ]; then
    echo "==== tail \$f (last $TAIL_LINES) ===="
    tail -n $TAIL_LINES "\$f"
  else
    echo "==== (no \$f here) ===="
  fi
done
echo "==== grep sutui-audit (last 50) ===="
grep '\[sutui-audit\]' backend.log mcp.log 2>/dev/null | tail -n 50 || true
EOF
