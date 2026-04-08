#!/usr/bin/env bash
# 本地执行：SSH 到 .env.deploy 中的主机，汇总 lobster.db 用户积分与流水
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

run_sql() {
  local sql="$1"
  bash "$ROOT/scripts/ssh_run_remote.sh" "sqlite3 /root/lobster_server/lobster.db $(printf '%q' "$sql")"
}

echo "=== 用户数 ==="
run_sql "SELECT COUNT(1) AS user_count FROM users;"

echo ""
echo "=== 各用户：当前余额 | 历史总消耗(扣费合计) | 历史总入账(充值等) ==="
run_sql "SELECT u.id, u.email, printf('%.4f', u.credits) AS balance, printf('%.4f', COALESCE(SUM(CASE WHEN l.delta < 0 THEN -l.delta ELSE 0 END), 0)) AS total_consumed, printf('%.4f', COALESCE(SUM(CASE WHEN l.delta > 0 THEN l.delta ELSE 0 END), 0)) AS total_credited FROM users u LEFT JOIN credit_ledger l ON l.user_id = u.id GROUP BY u.id, u.email, u.credits ORDER BY CAST(total_consumed AS REAL) DESC, u.id;"

echo ""
echo "=== 全站扣费流水按类型 ==="
run_sql "SELECT entry_type, printf('%.4f', SUM(-delta)) AS consumed_sum, COUNT(1) AS n FROM credit_ledger WHERE delta < 0 GROUP BY entry_type ORDER BY consumed_sum DESC;"
