#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bash "$ROOT/scripts/ssh_run_remote.sh" 'sqlite3 /root/lobster_server/lobster.db "SELECT id, email FROM users WHERE brand_mark IS NULL OR brand_mark = '"'"''"'"' ORDER BY id;"'
