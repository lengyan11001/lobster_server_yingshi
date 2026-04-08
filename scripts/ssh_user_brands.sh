#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bash "$ROOT/scripts/ssh_run_remote.sh" 'sqlite3 /root/lobster_server/lobster.db "SELECT brand_mark, COUNT(1) AS n FROM users GROUP BY brand_mark ORDER BY n DESC;"'
