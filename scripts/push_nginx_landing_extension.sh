#!/usr/bin/env bash
# 把 INSclaw 落地页 nginx 反代规则（/landing/ + /）推到宝塔 extension 目录并 reload nginx。
# 与 push_nginx_client_extension.sh 同款流程，只换源/目标文件。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/scripts/nginx_extension_api_51ins_landing.conf"
DEST="/www/server/panel/vhost/nginx/extension/api.51ins.com/landing_lobster_proxy.conf"
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
