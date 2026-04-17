#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
bash scripts/ssh_run_remote.sh 'echo "=== backend.log 422 / sora (last 5000 lines, tail grep) ==="
tail -5000 /root/lobster_server/backend.log 2>/dev/null | grep -E "422|sora|image-to-video|video\.generate|tasks/create" | tail -80 || true
echo "=== mcp.log 422 / sora ==="
tail -8000 /root/lobster_server/mcp.log 2>/dev/null | grep -E "422|sora|image-to-video|video\.generate|Sora" | tail -80 || true
echo "=== nginx 422 ==="
grep " 422 " /var/log/nginx/access.log 2>/dev/null | tail -30 || true'
