"""Quick Fubei API connectivity test."""
import hashlib, json, uuid, requests

APP_ID = "2026040989899946"
APP_SECRET = "b0acb6b42a95f23e5c152280f3e5fc32"
GATEWAY = "https://shq-api.51fubei.com/gateway/agent"

def fubei_sign(params, secret):
    parts = []
    for k in sorted(params.keys()):
        if k == "sign":
            continue
        parts.append(f"{k}={params[k]}")
    raw = "&".join(parts) + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()

def call(method, biz):
    nonce = uuid.uuid4().hex[:24]
    body = {
        "app_id": APP_ID,
        "method": method,
        "format": "json",
        "sign_method": "md5",
        "nonce": nonce,
        "version": "1.0",
        "biz_content": json.dumps(biz, ensure_ascii=False),
    }
    body["sign"] = fubei_sign(body, APP_SECRET)
    resp = requests.post(GATEWAY, json=body, headers={"Content-Type": "application/json; charset=utf-8"}, timeout=15)
    return resp.json()

print("=== 1. Query non-existent order ===")
r1 = call("fbpay.order.query", {"merchant_order_sn": "NONEXIST_123"})
print(f"  code={r1.get('result_code')}, msg={r1.get('result_message')}")

print("\n=== 2. Precreate 0.01 yuan (no store_id) ===")
r2 = call("fbpay.order.precreate", {
    "merchant_order_sn": f"TEST_{uuid.uuid4().hex[:12]}",
    "total_amount": 0.01,
    "body": "test-1fen",
})
print(f"  code={r2.get('result_code')}, msg={r2.get('result_message')}")
if r2.get("data"):
    print(f"  data keys: {list(r2['data'].keys()) if isinstance(r2['data'], dict) else r2['data']}")

print("\n=== 3. Precreate 0.01 yuan (with notify_url) ===")
r3 = call("fbpay.order.precreate", {
    "merchant_order_sn": f"TEST_{uuid.uuid4().hex[:12]}",
    "total_amount": 0.01,
    "body": "test-1fen",
    "notify_url": "https://example.com/api/recharge/fubei-notify",
})
print(f"  code={r3.get('result_code')}, msg={r3.get('result_message')}")
if r3.get("data"):
    d = r3["data"]
    if isinstance(d, dict):
        print(f"  qr_code: {d.get('qr_code', 'N/A')}")
        print(f"  order_sn: {d.get('order_sn', 'N/A')}")
    else:
        print(f"  data: {d}")

print("\nDone.")
