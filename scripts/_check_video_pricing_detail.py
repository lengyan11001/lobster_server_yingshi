"""Show full pricing details for models with per_second/None base_price."""
import httpx, json
from urllib.parse import quote

models = [
    "wan/v2.7/text-to-video",
    "xai/grok-imagine-video/text-to-video",
    "fal-ai/veo3.1",
    "fal-ai/sora-2/text-to-video",
    "st-ai/super-seed2",
    "fal-ai/vidu/q3/text-to-video",
    "ark/seedance-2.0",
]

base = "https://api.xskill.ai"
for m in models:
    safe = quote(m, safe="")
    url = f"{base}/api/v3/models/{safe}/docs?lang=zh"
    try:
        r = httpx.get(url, timeout=15.0)
        j = r.json()
        data = j.get("data", {})
        pricing = data.get("pricing", {})
        print(f"\n=== {m} ===")
        print(json.dumps(pricing, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"\n=== {m} === ERROR: {e}")
