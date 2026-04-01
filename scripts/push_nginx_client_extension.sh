#!/usr/bin/env bash
# 将 nginx_extension_api_51ins_client.conf 写入大陆机 api.51ins.com 的 extension 并 reload
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
