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
cd /opt/lobster-server

echo "=== 测试 fbpay.fixed.qrcode.create (含 expired_time) ==="
.venv/bin/python3 -c "
import sys, json, asyncio, time, hashlib, uuid
from datetime import datetime, timedelta
sys.path.insert(0, '.')
from backend.app.core.config import settings

app_id = str(getattr(settings, 'fubei_app_id', '') or '').strip()
app_secret = str(getattr(settings, 'fubei_app_secret', '') or '').strip()
store_id = str(getattr(settings, 'fubei_store_id', '') or '').strip()

import httpx

def fubei_sign(params, secret):
    parts = []
    for k in sorted(params.keys()):
        if k == 'sign':
            continue
        parts.append(f'{k}={params[k]}')
    raw = '&'.join(parts) + secret
    return hashlib.md5(raw.encode('utf-8')).hexdigest().upper()

async def test_api(method, biz, label=''):
    nonce = uuid.uuid4().hex[:24]
    body = {
        'app_id': app_id,
        'method': method,
        'format': 'json',
        'sign_method': 'md5',
        'nonce': nonce,
        'version': '1.0',
        'biz_content': json.dumps(biz, ensure_ascii=False),
    }
    body['sign'] = fubei_sign(body, app_secret)
    url = 'https://shq-api.51fubei.com/gateway/agent'
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        resp = await client.post(url, json=body, headers={'Content-Type': 'application/json; charset=utf-8'})
    result = resp.json()
    print(f'--- {label} ---')
    print(json.dumps(result, indent=2, ensure_ascii=False)[:1000])
    print()

async def main():
    tsn = 'TEST_' + str(int(time.time()))
    expired = (datetime.now() + timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')

    # Test 1: fixed.qrcode.create with expired_time
    await test_api('fbpay.fixed.qrcode.create', {
        'merchant_order_sn': tsn + '_fqc1',
        'store_id': int(store_id),
        'total_amount': 0.01,
        'body': 'test',
        'expired_time': expired,
        'notify_url': 'https://example.com/notify',
        'success_url': 'https://example.com/success',
        'fail_url': 'https://example.com/fail',
        'cancel_url': 'https://example.com/cancel',
    }, 'fixed.qrcode.create 含expired_time')

    # Test 2: try constructing QR URL from precreate sn
    await test_api('fbpay.order.precreate', {
        'merchant_order_sn': tsn + '_pre2',
        'store_id': int(store_id),
        'total_amount': 0.01,
        'body': 'test',
        'notify_url': 'https://example.com/notify',
        'success_url': 'https://example.com/success',
        'fail_url': 'https://example.com/fail',
        'cancel_url': 'https://example.com/cancel',
    }, 'precreate(参考sn)')

asyncio.run(main())
"
REMOTE
