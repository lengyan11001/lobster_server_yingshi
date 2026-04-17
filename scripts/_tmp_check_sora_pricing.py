import httpx
import json

variants = [
    'fal-ai/sora-2/text-to-video',
    'fal-ai/sora-2/image-to-video',
    'fal-ai/sora-2/vip/text-to-video',
    'fal-ai/sora-2/vip/image-to-video',
    'fal-ai/sora-2/text-to-video/pro',
    'fal-ai/sora-2/image-to-video/pro',
]

for v in variants:
    try:
        r = httpx.get(f'https://api.xskill.ai/api/v3/models/{v}/docs', params={'lang': 'zh'}, timeout=20)
        if r.status_code == 200:
            p = r.json().get('data', {}).get('pricing', {})
            bp = p.get('base_price')
            pt = p.get('price_type')
            exs = p.get('examples', [])
            first_ex = exs[0] if exs else None
            print(f'{v}:')
            print(f'  base_price={bp}, price_type={pt}')
            if first_ex:
                print(f'  first_example: {first_ex}')
            print()
        else:
            print(f'{v}: HTTP {r.status_code}\n')
    except Exception as e:
        print(f'{v}: error {e}\n')
