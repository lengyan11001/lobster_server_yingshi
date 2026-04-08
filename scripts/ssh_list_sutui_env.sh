#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bash "$ROOT/scripts/ssh_run_remote.sh" 'grep -E "^SUTUI_SERVER" /root/lobster_server/.env 2>/dev/null | sed "s/=.*/=<masked>/" || echo "(no .env or no SUTUI_SERVER lines)"'
