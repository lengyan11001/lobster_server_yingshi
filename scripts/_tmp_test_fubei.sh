#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOBSTER_DEPLOY_HOST=$(grep '^LOBSTER_DEPLOY_HOST=' "$ROOT/.env.deploy" | head -1 | cut -d= -f2-)
LOBSTER_DEPLOY_SSH_KEY=$(grep '^LOBSTER_DEPLOY_SSH_KEY=' "$ROOT/.env.deploy" | head -1 | cut -d= -f2-)
LOBSTER_DEPLOY_REMOTE_DIR=$(grep '^LOBSTER_DEPLOY_REMOTE_DIR=' "$ROOT/.env.deploy" | head -1 | cut -d= -f2-)
LOBSTER_SSH_KEY_PASSPHRASE=$(grep '^LOBSTER_SSH_KEY_PASSPHRASE=' "$ROOT/.env.deploy" | head -1 | cut -d= -f2-)
export LOBSTER_DEPLOY_HOST LOBSTER_DEPLOY_SSH_KEY LOBSTER_DEPLOY_REMOTE_DIR LOBSTER_SSH_KEY_PASSPHRASE

: "${LOBSTER_DEPLOY_HOST:?missing}"
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
cd /opt/lobster-server

echo "=== 测试付呗预下单 ==="
TS=$(date +%s)
.venv/bin/python3 -c "
import sys, json, asyncio, time
sys.path.insert(0, '.')
from backend.app.services.fubei_pay import fubei_precreate, fubei_configured

print('fubei_configured:', fubei_configured())

async def test():
    try:
        tsn = 'TEST_' + str(int(time.time())) + '_001'
        result = await fubei_precreate(
            merchant_order_sn=tsn,
            total_amount=0.01,
            body='test-0.01',
            notify_url='https://example.com/notify',
            success_url='https://example.com/success',
            fail_url='https://example.com/fail',
            cancel_url='https://example.com/cancel',
        )
        print('result_code:', result.get('result_code'))
        print('result_message:', result.get('result_message'))
        data = result.get('data')
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except:
                pass
        if data:
            print('data keys:', list(data.keys()) if isinstance(data, dict) else type(data))
            if isinstance(data, dict):
                qr = data.get('qr_code') or data.get('code_url') or data.get('qr_url') or ''
                print('qr_code:', qr[:120] if qr else 'N/A')
                print('order_sn:', data.get('order_sn', ''))
        else:
            print('data: None')
        print('full result:', json.dumps(result, indent=2, ensure_ascii=False)[:600])
    except Exception as e:
        print('ERROR:', type(e).__name__, str(e))

asyncio.run(test())
"
REMOTE
