#!/usr/bin/env bash
# 一次性：远端有本地改动导致 pull 失败时，备份后 stash、拉 main、重启（与 deploy_from_local 同一套 pull+restart）
set -euo pipefail
ROOT="${LOBSTER_DEPLOY_REMOTE_DIR:-/root/lobster_server}"
cd "$ROOT"
BACK="${HOME}/.lobster_deploy_backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACK"
cp -a backend/app/api/auth.py backend/app/core/config.py "$BACK/" 2>/dev/null || true
cp -a client_static/client_code/manifest.json "$BACK/" 2>/dev/null || true
if [ -f backend/app/services/sms_ihuyi.py ]; then
  if ! git ls-files --error-unmatch backend/app/services/sms_ihuyi.py >/dev/null 2>&1; then
    cp -a backend/app/services/sms_ihuyi.py "$BACK/sms_ihuyi.py.untracked"
    rm -f backend/app/services/sms_ihuyi.py
  fi
fi
BUND="$ROOT/client_static/client_code/bundles"
if [ -d "$BUND" ]; then
  mkdir -p "$BACK/bundles_untracked"
  for z in "$BUND"/*.zip; do
    [ -f "$z" ] || continue
    rel="client_static/client_code/bundles/$(basename "$z")"
    git ls-files --error-unmatch "$rel" >/dev/null 2>&1 && continue
    mv -f "$z" "$BACK/bundles_untracked/"
  done
fi
# 额外路径（海外机等）：环境变量 EXTRA_STASH="a b c"
paths=(backend/app/api/auth.py backend/app/core/config.py client_static/client_code/manifest.json)
# shellcheck disable=2206
[ -n "${EXTRA_STASH:-}" ] && paths+=($EXTRA_STASH)
git stash push -m "pre-standard-deploy $(date -Iseconds)" -- "${paths[@]}" || true
git fetch origin main
git pull origin main
bash scripts/server_update_and_restart.sh
echo "[完成] 备份在: $BACK（含 stash；需旧版 auth/config 可 git stash list / stash pop）"
