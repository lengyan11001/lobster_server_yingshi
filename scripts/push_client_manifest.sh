#!/usr/bin/env bash
# 将本机 client_static/client_code/manifest.json 写入远端（manifest 已 gitignore，需单独同步）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
set -a
# shellcheck source=../.env.deploy
. "$ROOT/.env.deploy"
set +a
DEST="${LOBSTER_DEPLOY_REMOTE_DIR}/client_static/client_code/manifest.json"
"$ROOT/scripts/ssh_run_remote.sh" "cat > \"$DEST\"" < "$ROOT/client_static/client_code/manifest.json"
echo "[ok] pushed manifest to $LOBSTER_DEPLOY_HOST:$DEST"
