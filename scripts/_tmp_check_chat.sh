#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOBSTER_DEPLOY_HOST=$(grep '^LOBSTER_DEPLOY_HOST=' "$ROOT/.env.deploy" | head -1 | cut -d= -f2-)
LOBSTER_DEPLOY_SSH_KEY=$(grep '^LOBSTER_DEPLOY_SSH_KEY=' "$ROOT/.env.deploy" | head -1 | cut -d= -f2-)
LOBSTER_SSH_KEY_PASSPHRASE=$(grep '^LOBSTER_SSH_KEY_PASSPHRASE=' "$ROOT/.env.deploy" | head -1 | cut -d= -f2-)
export LOBSTER_DEPLOY_HOST LOBSTER_DEPLOY_SSH_KEY LOBSTER_SSH_KEY_PASSPHRASE

SSH_BASE="-o StrictHostKeyChecking=accept-new"
_ssh_agent_has_keys() { [ -n "${SSH_AUTH_SOCK:-}" ] && ssh-add -l >/dev/null 2>&1; }
_DEPLOY_SSH_AGENT_STARTED=0
_deploy_cleanup_ssh_agent() { [ "$_DEPLOY_SSH_AGENT_STARTED" = 1 ] && { eval "$(ssh-agent -k)" 2>/dev/null || true; _DEPLOY_SSH_AGENT_STARTED=0; }; }
if ! _ssh_agent_has_keys; then
  if [ -n "${LOBSTER_DEPLOY_SSH_KEY:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -n "${LOBSTER_SSH_KEY_PASSPHRASE:-}" ]; then
    AP="$(mktemp)"; { echo '#!/usr/bin/env sh'; echo "printf '%s\\n' '$LOBSTER_SSH_KEY_PASSPHRASE'"; } > "$AP"; chmod +x "$AP"
    trap 'rm -f "$AP"; _deploy_cleanup_ssh_agent' EXIT
    eval "$(ssh-agent -s)"; _DEPLOY_SSH_AGENT_STARTED=1
    export SSH_ASKPASS_REQUIRE=force SSH_ASKPASS="$AP" DISPLAY="${DISPLAY:-localhost:0}"
    ssh-add "$LOBSTER_DEPLOY_SSH_KEY"; rm -f "$AP"; trap _deploy_cleanup_ssh_agent EXIT
  fi
fi
SSH_OPTS="$SSH_BASE"
! _ssh_agent_has_keys && [ -n "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && SSH_OPTS="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_BASE"

ssh $SSH_OPTS "$LOBSTER_DEPLOY_HOST" bash -s <<'REMOTE'
echo "=== 服务状态 ==="
systemctl status lobster-backend --no-pager -l 2>/dev/null | head -20
echo ""

echo "=== backend 最近 journal 日志 ==="
journalctl -u lobster-backend --no-pager -n 80 2>/dev/null | tail -80

echo ""
echo "=== backend.log 最近内容 ==="
tail -100 /opt/lobster-server/backend.log 2>/dev/null
echo "--- end ---"

echo ""
echo "=== 检查 DeepSeek 配置 ==="
cd /opt/lobster-server
.venv/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from backend.app.core.config import settings

print('deepseek_api_key:', ('*****' + str(getattr(settings, 'deepseek_api_key', ''))[-6:]) if getattr(settings, 'deepseek_api_key', '') else 'NOT SET')
print('deepseek_base_url:', getattr(settings, 'deepseek_base_url', 'NOT SET'))
print('deepseek_model:', getattr(settings, 'deepseek_model', 'NOT SET'))

# check sutui fallback config
print('sutui_api_key:', ('*****' + str(getattr(settings, 'sutui_api_key', ''))[-6:]) if getattr(settings, 'sutui_api_key', '') else 'NOT SET')
print('sutui_base_url:', getattr(settings, 'sutui_base_url', 'NOT SET'))

# check chat-related settings
for attr in dir(settings):
    if 'chat' in attr.lower() or 'llm' in attr.lower() or 'deep' in attr.lower() or 'model' in attr.lower():
        val = getattr(settings, attr, None)
        if val and not callable(val) and not attr.startswith('_'):
            if 'key' in attr.lower() or 'secret' in attr.lower():
                print(f'{attr}: *****{str(val)[-6:]}')
            else:
                print(f'{attr}: {val}')
" 2>&1
REMOTE
