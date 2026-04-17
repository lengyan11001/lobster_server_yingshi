#!/usr/bin/env bash
# [已废弃] 旧宝塔面板用；新服务器（42.194.209.150）nginx 配置在 /etc/nginx/sites-available/lobster
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/scripts/nginx_extension_api_51ins_client.conf"
DEST="/www/server/panel/vhost/nginx/extension/api.51ins.com/client_lobster_proxy.conf"
cd "$ROOT"
if [ -f "$ROOT/.env.deploy" ]; then
  set -a
  # shellcheck source=../.env.deploy
  . "$ROOT/.env.deploy"
  set +a
fi
: "${LOBSTER_SSH_KEY_PASSPHRASE:?}"
: "${LOBSTER_DEPLOY_HOST:?}"
: "${LOBSTER_DEPLOY_SSH_KEY:?}"
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
ssh -o StrictHostKeyChecking=accept-new "$LOBSTER_DEPLOY_HOST" "sudo tee $DEST >/dev/null" < "$SRC"
ssh -o StrictHostKeyChecking=accept-new "$LOBSTER_DEPLOY_HOST" "sudo nginx -t && sudo nginx -s reload && echo OK_reloaded"
