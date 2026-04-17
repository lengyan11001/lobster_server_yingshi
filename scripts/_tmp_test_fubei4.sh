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

echo "=== 测试不同API方法名 ==="
.venv/bin/python3 -c "
import sys, json, asyncio, time, hashlib, uuid
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

async def test_method(method, biz, version='1.0', label=''):
    nonce = uuid.uuid4().hex[:24]
    body = {
        'app_id': app_id,
        'method': method,
        'format': 'json',
        'sign_method': 'md5',
        'nonce': nonce,
        'version': version,
        'biz_content': json.dumps(biz, ensure_ascii=False),
    }
    body['sign'] = fubei_sign(body, app_secret)
    url = 'https://shq-api.51fubei.com/gateway/agent'
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        resp = await client.post(url, json=body, headers={'Content-Type': 'application/json; charset=utf-8'})
    result = resp.json()
    print(f'--- {label} (method={method} v={version}) ---')
    print(json.dumps(result, indent=2, ensure_ascii=False)[:500])
    print()

async def main():
    base_biz = {
        'merchant_order_sn': 'TEST_' + str(int(time.time())) + '_a',
        'total_amount': 0.01,
        'store_id': int(store_id),
        'body': 'test',
        'notify_url': 'https://example.com/notify',
        'success_url': 'https://example.com/success',
        'fail_url': 'https://example.com/fail',
        'cancel_url': 'https://example.com/cancel',
    }

    # Test 1: version 2.0
    biz1 = dict(base_biz)
    biz1['merchant_order_sn'] = 'TEST_' + str(int(time.time())) + '_v2'
    await test_method('fbpay.order.precreate', biz1, version='2.0', label='v2.0')

    # Test 2: fbpay.pay.precreate
    biz2 = dict(base_biz)
    biz2['merchant_order_sn'] = 'TEST_' + str(int(time.time())) + '_pay'
    await test_method('fbpay.pay.precreate', biz2, label='fbpay.pay.precreate')

    # Test 3: fbpay.order.create (not precreate)
    biz3 = dict(base_biz)
    biz3['merchant_order_sn'] = 'TEST_' + str(int(time.time())) + '_create'
    await test_method('fbpay.order.create', biz3, label='fbpay.order.create')

    # Test 4: fbpay.scanpay.precreate
    biz4 = dict(base_biz)
    biz4['merchant_order_sn'] = 'TEST_' + str(int(time.time())) + '_scan'
    await test_method('fbpay.scanpay.precreate', biz4, label='fbpay.scanpay.precreate')

    # Test 5: fbpay.qrcode.create (fixed QR code)
    biz5 = {
        'store_id': int(store_id),
        'total_amount': 0.01,
        'notify_url': 'https://example.com/notify',
        'success_url': 'https://example.com/success',
        'fail_url': 'https://example.com/fail',
        'cancel_url': 'https://example.com/cancel',
    }
    await test_method('fbpay.fixed.qrcode.create', biz5, label='fbpay.fixed.qrcode.create')

asyncio.run(main())
"
REMOTE
