#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -f "$ROOT/.env.deploy" ]; then
  set -a
  . "$ROOT/.env.deploy"
  set +a
fi
: "${LOBSTER_DEPLOY_HOST:?missing LOBSTER_DEPLOY_HOST in .env.deploy}"

REMOTE_DIR="${LOBSTER_DEPLOY_REMOTE_DIR:-/opt/lobster-server}"

SSH_BASE="-o StrictHostKeyChecking=accept-new"
_ssh_agent_has_keys() {
  [ -n "${SSH_AUTH_SOCK:-}" ] && ssh-add -l >/dev/null 2>&1
}

_DEPLOY_SSH_AGENT_STARTED=0
_deploy_cleanup_ssh_agent() {
  if [ "$_DEPLOY_SSH_AGENT_STARTED" = 1 ]; then
    eval "$(ssh-agent -k)" 2>/dev/null || true
    _DEPLOY_SSH_AGENT_STARTED=0
  fi
}

if ! _ssh_agent_has_keys; then
  if [ -n "${LOBSTER_DEPLOY_SSH_KEY:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -n "${LOBSTER_SSH_KEY_PASSPHRASE:-}" ]; then
    AP="$(mktemp)"
    {
      echo '#!/usr/bin/env sh'
      echo 'printf %s\\n "$LOBSTER_SSH_KEY_PASSPHRASE"'
    } > "$AP"
    chmod +x "$AP"
    trap 'rm -f "$AP"; _deploy_cleanup_ssh_agent' EXIT
    eval "$(ssh-agent -s)"
    _DEPLOY_SSH_AGENT_STARTED=1
    export SSH_ASKPASS_REQUIRE=force
    export SSH_ASKPASS="$AP"
    export DISPLAY="${DISPLAY:-localhost:0}"
    ssh-add "$LOBSTER_DEPLOY_SSH_KEY"
    rm -f "$AP"
    trap _deploy_cleanup_ssh_agent EXIT
  fi
fi

SSH_OPTS_MAIN="$SSH_BASE"
if ! _ssh_agent_has_keys; then
  [ -n "${LOBSTER_DEPLOY_SSH_KEY:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && SSH_OPTS_MAIN="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_BASE"
fi

ssh $SSH_OPTS_MAIN "$LOBSTER_DEPLOY_HOST" bash -s <<'REMOTE_EOF'
cd /opt/lobster-server

echo "========== 1. backend service status =========="
systemctl status lobster-backend.service 2>/dev/null | head -15 || echo "(no service)"

echo ""
echo "========== 2. MCP port check =========="
ss -tlnp | grep 8001 || echo "(port 8001 not listening)"

echo ""
echo "========== 3. app.log last 150 lines =========="
tail -n 150 logs/app.log 2>/dev/null || echo "(no app.log)"

echo ""
echo "========== 4. backend.log last 80 lines =========="
tail -n 80 backend.log 2>/dev/null || echo "(no backend.log)"

echo ""
echo "========== 5. mcp.log last 50 lines =========="
tail -n 50 mcp.log 2>/dev/null || echo "(no mcp.log)"

echo ""
echo "========== 6. journalctl lobster-backend last 80 =========="
journalctl -u lobster-backend.service -n 80 --no-pager 2>/dev/null || echo "(no journal)"

echo ""
echo "========== 7. grep video/sora/error in app.log (last 50 matches) =========="
grep -inE 'video|sora|错误|error|exception|traceback|No module|backend\.mcp' logs/app.log 2>/dev/null | tail -50 || echo "(nothing found)"

echo ""
echo "========== 8. grep video/sora/error in backend.log (last 30 matches) =========="
grep -inE 'video|sora|错误|error|exception|traceback|No module|backend\.mcp' backend.log 2>/dev/null | tail -30 || echo "(nothing found)"

echo ""
echo "========== 9. Check code for backend.mcp import =========="
grep -rn 'backend\.mcp\|from backend.mcp\|import backend.mcp' backend/ mcp/ 2>/dev/null || echo "(no backend.mcp import found in code)"

echo ""
echo "========== 10. Check run.py entry point =========="
head -40 backend/run.py 2>/dev/null || echo "(no run.py)"

echo ""
echo "========== 11. Check mcp service unit file =========="
cat /etc/systemd/system/lobster-mcp.service 2>/dev/null || echo "(no lobster-mcp.service unit file)"

echo ""
echo "========== 12. Check backend service unit file =========="
cat /etc/systemd/system/lobster-backend.service 2>/dev/null || echo "(no lobster-backend.service unit file)"

echo ""
echo "========== 13. Quick MCP health check =========="
curl -s -m 5 -X POST http://127.0.0.1:8001/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
tools=[t['name'] for t in d.get('result',{}).get('tools',[])]
print(f'MCP OK: {len(tools)} tools: {tools}')
" 2>/dev/null || echo "MCP health check failed"

echo ""
echo "========== DONE =========="
REMOTE_EOF
