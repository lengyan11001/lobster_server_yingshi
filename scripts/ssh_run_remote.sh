#!/usr/bin/env bash
# 用 .env.deploy 登录 LOBSTER_DEPLOY_HOST，在远端执行一条命令（参数为整条 remote 命令）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -f "$ROOT/.env.deploy" ]; then
  set -a
  # shellcheck source=../.env.deploy
  . "$ROOT/.env.deploy"
  set +a
fi
: "${LOBSTER_SSH_KEY_PASSPHRASE:?missing LOBSTER_SSH_KEY_PASSPHRASE in .env.deploy}"
: "${LOBSTER_DEPLOY_HOST:?missing LOBSTER_DEPLOY_HOST}"
: "${LOBSTER_DEPLOY_SSH_KEY:?missing LOBSTER_DEPLOY_SSH_KEY}"
AP="$(mktemp)"
{
  echo '#!/usr/bin/env sh'
  echo 'printf %s\\n "$LOBSTER_SSH_KEY_PASSPHRASE"'
} > "$AP"
chmod +x "$AP"
eval "$(ssh-agent -s)"
trap 'rm -f "$AP"; eval "$(ssh-agent -k)" 2>/dev/null || true' EXIT
export SSH_ASKPASS_REQUIRE=force
export SSH_ASKPASS="$AP"
export DISPLAY="${DISPLAY:-localhost:0}"
ssh-add "$LOBSTER_DEPLOY_SSH_KEY"
ssh -o StrictHostKeyChecking=accept-new "$LOBSTER_DEPLOY_HOST" "$@"
