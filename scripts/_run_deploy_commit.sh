#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
MSG_FILE=$(mktemp)
printf '%s\n' 'deploy: server chat API' > "$MSG_FILE"
git commit --file="$MSG_FILE"
rm -f "$MSG_FILE"
git push origin main
bash scripts/deploy_from_local.sh
