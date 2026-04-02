#!/usr/bin/env bash
# 与 README-部署.md 一致：git push origin main → SSH 远端 git pull + server_update_and_restart.sh
# 加密私钥在非交互环境：先 export LOBSTER_SSH_KEY_PASSPHRASE=口令，再执行本脚本（勿把口令写入仓库）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/.env.deploy" ]; then
  set -a
  # shellcheck source=../.env.deploy
  . "$ROOT/.env.deploy"
  set +a
fi

: "${LOBSTER_SSH_KEY_PASSPHRASE:?请先 export LOBSTER_SSH_KEY_PASSPHRASE（加密私钥口令）}"

if [ -z "${LOBSTER_DEPLOY_SSH_KEY:-}" ] || [ ! -r "$LOBSTER_DEPLOY_SSH_KEY" ]; then
  echo "[ERR] .env.deploy 中 LOBSTER_DEPLOY_SSH_KEY 不可读（Windows 示例：/d/maczhuji）"
  exit 1
fi

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

echo ">>> git push origin main"
git push origin main

echo ">>> bash scripts/deploy_from_local.sh"
bash "$ROOT/scripts/deploy_from_local.sh"
