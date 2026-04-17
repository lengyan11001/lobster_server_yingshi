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
    { echo '#!/usr/bin/env sh'; echo 'printf %s\\n "$LOBSTER_SSH_KEY_PASSPHRASE"'; } > "$AP"
    chmod +x "$AP"
    trap 'rm -f "$AP"; _deploy_cleanup_ssh_agent' EXIT
    eval "$(ssh-agent -s)"
    _DEPLOY_SSH_AGENT_STARTED=1
    export SSH_ASKPASS_REQUIRE=force SSH_ASKPASS="$AP" DISPLAY="${DISPLAY:-localhost:0}"
    ssh-add "$LOBSTER_DEPLOY_SSH_KEY"
    rm -f "$AP"
    trap _deploy_cleanup_ssh_agent EXIT
  fi
fi
SSH_OPTS_MAIN="$SSH_BASE"
if ! _ssh_agent_has_keys; then
  [ -n "${LOBSTER_DEPLOY_SSH_KEY:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && SSH_OPTS_MAIN="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_BASE"
fi

echo "=== SCP chat.py to server ==="
scp $SSH_OPTS_MAIN backend/app/api/chat.py "$LOBSTER_DEPLOY_HOST:$REMOTE_DIR/backend/app/api/chat.py"
echo "=== SCP done ==="

echo "=== Restart backend ==="
ssh $SSH_OPTS_MAIN "$LOBSTER_DEPLOY_HOST" bash -s <<'REMOTE_EOF'
cd /opt/lobster-server
echo "Restarting lobster-backend..."
sudo systemctl restart lobster-backend.service
sleep 3
systemctl status lobster-backend.service | head -10
echo ""
echo "Clearing old logs..."
> logs/app.log 2>/dev/null && echo "app.log cleared" || echo "failed"
> backend.log 2>/dev/null && echo "backend.log cleared" || echo "failed"
echo ""
echo "Check MCP health..."
curl -s -m 5 -X POST http://127.0.0.1:8001/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
tools=[t['name'] for t in d.get('result',{}).get('tools',[])]
print(f'MCP OK: {len(tools)} tools')
" 2>/dev/null || echo "MCP health check failed (may need more time)"
echo "=== DONE ==="
REMOTE_EOF
