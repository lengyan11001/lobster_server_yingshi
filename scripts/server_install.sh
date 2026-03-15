#!/usr/bin/env bash
# 云服务器首次：安装 Python 依赖（创建 venv + pip install）
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -f "requirements.txt" ]; then
  echo "[ERR] 请在 lobster 项目根目录执行，或确保 requirements.txt 存在"
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "[1/2] 创建虚拟环境 .venv ..."
  python3 -m venv .venv
fi
echo "[2/2] 安装依赖 pip install -r requirements.txt ..."
"$ROOT/.venv/bin/pip" install -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "已复制 .env.example → .env，请编辑 .env 填写 SECRET_KEY、SUTUI_SERVER_TOKEN 等后执行："
  echo "  ./scripts/server_start.sh"
  exit 0
fi

echo "安装完成。启动：./scripts/server_start.sh"
