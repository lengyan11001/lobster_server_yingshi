#!/usr/bin/env bash
# 在本机 lobster-server 根目录执行；读取 .env.deploy（与 deploy_from_local.sh 相同 SSH 方式）
# 用法: bash scripts/admin_add_credits_remote.sh <邮箱片段或唯一匹配子串> [加积分数量]
# 例: bash scripts/admin_add_credits_remote.sh z717010460 10000
#
# SSH 约定见 README-部署.md、.env.deploy.example：LOBSTER_DEPLOY_HOST、LOBSTER_DEPLOY_SSH_KEY、LOBSTER_DEPLOY_REMOTE_DIR
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -f "$ROOT/.env.deploy" ]; then
  set -a
  # shellcheck source=../.env.deploy
  . "$ROOT/.env.deploy"
  set +a
fi
if [ -z "${LOBSTER_DEPLOY_HOST:-}" ]; then
  echo "缺少 .env.deploy 或未设置 LOBSTER_DEPLOY_HOST（见 README-部署.md）"
  exit 1
fi
HINT="${1:-}"
AMT="${2:-10000}"
REMOTE_DIR="${LOBSTER_DEPLOY_REMOTE_DIR:-/root/lobster_server}"
if [ -z "$HINT" ]; then
  echo "用法: $0 <邮箱/账号片段> [加积分]"
  exit 1
fi
SSH_BASE="-o StrictHostKeyChecking=accept-new"
SSH_OPTS="$SSH_BASE"
if [ -n "${LOBSTER_DEPLOY_SSH_KEY:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ]; then
  SSH_OPTS="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_BASE"
fi

ssh $SSH_OPTS "$LOBSTER_DEPLOY_HOST" bash -s -- "$HINT" "$AMT" "$REMOTE_DIR" <<'REMOTE'
set -euo pipefail
HINT="$1"
AMT="$2"
DIR="$3"
cd "$DIR"
if [ ! -f lobster.db ]; then
  echo "未找到 $PWD/lobster.db（若生产用 MySQL，请改 DATABASE_URL 后勿用本脚本）"
  exit 1
fi
echo "=== 匹配用户（变更前）==="
sqlite3 lobster.db "SELECT id, email, credits FROM users WHERE email LIKE '%${HINT}%' OR email = '${HINT}';"
N=$(sqlite3 lobster.db "SELECT count(*) FROM users WHERE email LIKE '%${HINT}%' OR email = '${HINT}';")
if [ "$N" != "1" ]; then
  echo "匹配行数=$N（必须为 1 才执行）。请改用更唯一子串。"
  exit 1
fi
sqlite3 lobster.db <<SQL
BEGIN;
UPDATE users SET credits = credits + ${AMT} WHERE email LIKE '%${HINT}%' OR email = '${HINT}';
INSERT INTO credit_ledger (user_id, delta, balance_after, entry_type, description, ref_type, ref_id, created_at)
SELECT id, ${AMT}, credits, 'recharge', '管理员手动加积分', 'admin', 'manual', datetime('now') FROM users WHERE email LIKE '%${HINT}%' OR email = '${HINT}';
COMMIT;
SQL
echo "=== 变更后 ==="
sqlite3 lobster.db "SELECT id, email, credits FROM users WHERE email LIKE '%${HINT}%' OR email = '${HINT}';"
echo "=== 最近一条流水 ==="
sqlite3 lobster.db "SELECT id, user_id, delta, balance_after, entry_type, description, created_at FROM credit_ledger WHERE user_id = (SELECT id FROM users WHERE email LIKE '%${HINT}%' OR email = '${HINT}' LIMIT 1) ORDER BY id DESC LIMIT 1;"
REMOTE
