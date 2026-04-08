#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="$ROOT/_last_deploy_output.txt"
./scripts/ssh_run_remote.sh 'cd /root/lobster_server && git fetch origin main && git pull origin main && bash scripts/server_update_and_restart.sh' >"$OUT" 2>&1
cat "$OUT"
