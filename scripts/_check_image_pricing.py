"""Query all image model pricing from Sutui/xskill API."""
import httpx, json
from urllib.parse import quote

models = [
    "jimeng-4.0",
    "jimeng-4.5",
    "fal-ai/flux-2/flash",
    "fal-ai/bytedance/seedream/v4.5/text-to-image",
    "fal-ai/bytedance/seedream/v4.5/edit",
    "fal-ai/nano-banana-pro",
    "kapon/gemini-3-pro-image-preview",
    "wan/v2.7/edit",
    "qwen-image-edit",
    "fal-ai/seedream/v3/text-to-image",
]

base = "https://api.xskill.ai"
print(f"{'Model ID':<55} {'Status':<12} {'Price Type':<20} {'Base Price'}")
print("-" * 110)

for m in models:
    safe = quote(m, safe="")
    url = f"{base}/api/v3/models/{safe}/docs?lang=zh"
    try:
        r = httpx.get(url, timeout=15.0)
        if r.status_code == 404:
            print(f"{m:<55} {'NO DOCS':<12}")
            continue
        j = r.json()
        code = j.get("code")
        if code != 200:
            print(f"{m:<55} {'ERR ' + str(code):<12}")
            continue
        data = j.get("data", {})
        pricing = data.get("pricing", {})
        if pricing:
            pt = pricing.get("price_type", "?")
            bp = pricing.get("base_price", "?")
            ps = pricing.get("per_second", "")
            extra = f" per_second={ps}" if ps else ""
            print(f"{m:<55} {'OK':<12} {pt:<20} {bp}{extra}")
        else:
            print(f"{m:<55} {'NO PRICING':<12}")
    except Exception as e:
        print(f"{m:<55} {'EXCEPTION':<12} {e}")
