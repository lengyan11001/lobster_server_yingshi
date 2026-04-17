"""Query all video model pricing from Sutui/xskill API."""
import httpx
from urllib.parse import quote

models = [
    "fal-ai/sora-2/text-to-video",
    "fal-ai/sora-2/image-to-video",
    "st-ai/super-seed2",
    "wan/v2.6/text-to-video",
    "wan/v2.6/image-to-video",
    "wan/v2.7/text-to-video",
    "wan/v2.7/image-to-video",
    "fal-ai/veo3.1",
    "fal-ai/kling-video/o3/pro/text-to-video",
    "fal-ai/kling-video/o3/pro/image-to-video",
    "xai/grok-imagine-video/text-to-video",
    "xai/grok-imagine-video/image-to-video",
    "fal-ai/minimax/hailuo-2.3/pro/text-to-video",
    "fal-ai/minimax/hailuo-2.3/pro/image-to-video",
    "fal-ai/vidu/q3/text-to-video",
    "fal-ai/vidu/q3/image-to-video",
    "jimeng-video-3.5-pro",
    "ark/seedance-2.0",
    "fal-ai/sora-2/vip/text-to-video",
    "fal-ai/sora-2/vip/image-to-video",
    "fal-ai/sora-2/text-to-video/pro",
    "fal-ai/sora-2/image-to-video/pro",
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
            print(f"{m:<55} {'OK':<12} {pt:<20} {bp}")
        else:
            print(f"{m:<55} {'NO PRICING':<12}")
    except Exception as e:
        print(f"{m:<55} {'EXCEPTION':<12} {e}")
