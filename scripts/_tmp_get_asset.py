import httpx
import json

asset_id = "ab505335c799"
base = "http://127.0.0.1:8000"

r = httpx.get(f"{base}/api/assets/{asset_id}", timeout=10)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    print(json.dumps(data, indent=2, ensure_ascii=False)[:1000])
else:
    print(r.text[:500])

r2 = httpx.get(f"{base}/api/assets", params={"q": asset_id}, timeout=10)
print(f"\nSearch status: {r2.status_code}")
if r2.status_code == 200:
    data2 = r2.json()
    print(json.dumps(data2, indent=2, ensure_ascii=False)[:1000])
else:
    print(r2.text[:500])
