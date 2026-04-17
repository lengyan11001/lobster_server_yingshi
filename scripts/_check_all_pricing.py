"""Query ALL model pricing from Sutui/xskill API."""
import httpx, json
from urllib.parse import quote

base = "https://api.xskill.ai"

# Get all models
r = httpx.get(f"{base}/api/v3/models?lang=zh", timeout=15.0)
models_data = r.json().get("data", {}).get("models", [])
model_names = [m["name"] for m in models_data]

# Also check sora-2 variants that might not be in the list
extra = [
    "fal-ai/sora-2/text-to-video",
    "fal-ai/sora-2/image-to-video",
    "fal-ai/sora-2/vip/text-to-video",
    "fal-ai/sora-2/vip/image-to-video",
    "fal-ai/sora-2/text-to-video/pro",
    "fal-ai/sora-2/image-to-video/pro",
    "xai/grok-imagine-video/text-to-video",
    "xai/grok-imagine-video/image-to-video",
    "fal-ai/kling-video/o3/pro/text-to-video",
    "fal-ai/kling-video/o3/pro/image-to-video",
    "fal-ai/kling-video/o3/text-to-video",
    "fal-ai/kling-video/o3/image-to-video",
    "fal-ai/kling-video/text-to-video",
    "fal-ai/kling-video/image-to-video",
]
for e in extra:
    if e not in model_names:
        model_names.append(e)

print(f"Total models to check: {len(model_names)}")
print(f"\n{'Model ID':<65} {'Status':<12} {'Price Type':<22} {'Base':<8} {'per_s'}")
print("-" * 120)

ok_count = 0
fail_count = 0
for m in sorted(model_names):
    safe = quote(m, safe="")
    url = f"{base}/api/v3/models/{safe}/docs?lang=zh"
    try:
        r = httpx.get(url, timeout=15.0)
        if r.status_code == 404:
            print(f"{m:<65} {'NO DOCS':<12}")
            fail_count += 1
            continue
        j = r.json()
        code = j.get("code")
        if code != 200:
            print(f"{m:<65} {'ERR ' + str(code):<12}")
            fail_count += 1
            continue
        data = j.get("data", {})
        pricing = data.get("pricing", {})
        if pricing:
            pt = pricing.get("price_type", "?")
            bp = pricing.get("base_price", "-")
            ps = pricing.get("per_second", "")
            print(f"{m:<65} {'OK':<12} {pt:<22} {str(bp):<8} {ps}")
            ok_count += 1
        else:
            print(f"{m:<65} {'NO PRICING':<12}")
            fail_count += 1
    except Exception as e:
        print(f"{m:<65} {'EXCEPTION':<12} {e}")
        fail_count += 1

print(f"\nSummary: {ok_count} OK, {fail_count} failed/no-pricing")
