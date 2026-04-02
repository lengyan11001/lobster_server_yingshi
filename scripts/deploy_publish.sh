#!/usr/bin/env bash
# 开发机：有未提交改动则提交 → push main → SSH 服务器 pull 并重启
# 需：git 已配置 remote；本机 lobster-server/.env.deploy（见 .env.deploy.example）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
  git add -A
  git commit -m "chore: deploy $(date +%Y%m%d-%H%M%S)"
fi

exec bash "$ROOT/scripts/deploy_server.sh"
