#!/usr/bin/env bash
# 开发机一键：推送 lobster_server 当前分支 → SSH 远端 git pull → 重启 Backend+MCP
# 依赖：仓库已 git commit；本机已配置 .env.deploy（见 .env.deploy.example）
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
echo "[deploy_server] git push origin main ..."
git push origin main
echo "[deploy_server] SSH 拉取并重启 ..."
exec bash "$ROOT/scripts/deploy_from_local.sh"
