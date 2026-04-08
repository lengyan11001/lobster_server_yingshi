#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bash "$ROOT/scripts/ssh_run_remote.sh" 'grep -E "^SUTUI_SERVER_TOKENS_BIHUO=|^SUTUI_SERVER_TOKENS_YINGSHI=" /root/lobster_server/.env'
