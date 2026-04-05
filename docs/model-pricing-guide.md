# xSkill API 模型价格信息获取指南

## 概览

通过两步 API 调用获取所有模型的价格信息：

1. **获取模型列表** — 拿到所有可用模型的 `id`
2. **获取模型详情** — 根据 `id` 查询包含定价在内的完整文档

> Base URL: `https://api.xskill.ai`

---

## 第一步：获取模型列表

### 请求

```
GET /api/v3/mcp/models?lang=en
```

无需鉴权，公开接口。`lang` 参数可选 `en`（英文）或 `zh`（中文）。

### cURL 示例

```bash
curl 'https://api.xskill.ai/api/v3/mcp/models?lang=en' \
  -H 'content-type: application/json'
```

### 响应结构

```json
{
  "code": 200,
  "data": {
    "models": [
      {
        "id": "jimeng-5.0",
        "name": "Jimeng 5.0 Flagship",
        "category": "image",       // image | video | audio | text
        "task_type": "t2i",         // t2i | i2i | t2v | i2v | v2v | t2a | stt | chat ...
        "description": "...",
        "isHot": true,
        "isNew": true,
        "stats": {
          "success_rate": 1.0,
          "total_tasks": 9
        }
      }
    ],
    "total": 121
  }
}
```

### 关键字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 模型唯一标识，用于第二步查询 |
| `category` | string | 分类：`image` / `video` / `audio` / `text` |
| `task_type` | string | 任务类型：`t2i`(文生图) `i2i`(图生图) `t2v`(文生视频) `i2v`(图生视频) `v2v`(视频生视频) `t2a`(文生音频) `stt`(语音转文) `chat`(对话) |
| `stats.success_rate` | float | 成功率 (0-1) |
| `stats.total_tasks` | int | 历史总任务数 |

---

## 第二步：获取模型详情（含价格）

### 请求

```
GET /api/v3/models/{model_id}/docs?lang=en
```

将 `{model_id}` 替换为第一步中获取的模型 `id`。含 `/` 的 id 无需转义，直接拼接即可。

### cURL 示例

```bash
# 图像模型
curl 'https://api.xskill.ai/api/v3/models/jimeng-5.0/docs?lang=en'

# 含斜杠的模型 id
curl 'https://api.xskill.ai/api/v3/models/fal-ai/sora-2/text-to-video/docs?lang=en'
```

### 响应结构

```json
{
  "code": 200,
  "data": {
    "id": "jimeng-5.0",
    "name": "Jimeng 5.0 Flagship",
    "category": "image",
    "task_type": "t2i",
    "description": "...",
    "fal_model": "jimeng-5.0",
    "params_schema": { ... },
    "pricing": { ... },
    "api_usage": { ... },
    "mcp_usage": { ... },
    "examples": [],
    "skills": []
  }
}
```

---

## 价格结构详解

`pricing` 字段包含完整的定价信息：

```json
{
  "pricing": {
    "base_price": 2,
    "price_type": "quantity_based",
    "price_description": "Priced by quantity: 2 credits/image",
    "price_unit": "credits",
    "price_factors": ["num_images (quantity)"],
    "vip_discount": null,
    "examples": [
      { "description": "Generate 1 images", "price": 2 },
      { "description": "Generate 4 images", "price": 8 }
    ]
  }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `base_price` | number | 基础单价（积分） |
| `price_type` | string | 计价方式（见下表） |
| `price_description` | string | 人类可读的价格描述 |
| `price_unit` | string | 计价单位，通常为 `credits`（积分） |
| `price_factors` | array | 影响最终价格的因素 |
| `vip_discount` | object/null | VIP 折扣信息，null 表示无折扣 |
| `examples` | array | 典型场景的价格示例 |

### 计价方式 (price_type)

| price_type | 说明 | 典型模型 |
|------------|------|----------|
| `quantity_based` | 按生成数量计费 | 图像模型（每张 N 积分） |
| `duration_based` | 按输出时长计费 | 视频模型（每秒 N 积分） |
| `fixed` | 固定价格 | Sora 2 Pub（每次 20 积分） |
| `token_based` | 按 token 用量计费 | LLM 文本模型 |
| `audio_duration_based` | 按音频时长计费 | TTS 语音合成 |

---

## 参数结构详解

`params_schema` 遵循 JSON Schema 规范，描述模型接受的所有参数：

```json
{
  "params_schema": {
    "type": "object",
    "properties": {
      "prompt": {
        "type": "string",
        "description": "Image description prompt",
        "examples": ["一只白色的猫在阳光下打瞌睡"]
      },
      "ratio": {
        "type": "string",
        "default": "1:1",
        "enum": ["1:1", "4:3", "3:4", "16:9", "9:16"]
      }
    },
    "required": ["prompt"]
  }
}
```

---

## API 调用示例

`api_usage` 字段提供了完整的任务提交和查询流程：

### 提交任务

```bash
curl -X POST 'https://api.xskill.ai/api/v3/tasks/create' \
  -H 'Authorization: Bearer sk-xxx' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "jimeng-5.0",
    "params": {
      "prompt": "A beautiful sunset over the ocean"
    }
  }'
```

### 查询结果

```bash
curl -X POST 'https://api.xskill.ai/api/v3/tasks/query' \
  -H 'Authorization: Bearer sk-xxx' \
  -H 'Content-Type: application/json' \
  -d '{
    "task_id": "550e8400-e29b-41d4-a716-446655440000"
  }'
```

---

## Python 完整示例

### 批量获取所有模型价格

```python
import requests
import json
import time

BASE_URL = "https://api.xskill.ai"


def get_all_models(lang="en"):
    """获取全部模型列表"""
    resp = requests.get(f"{BASE_URL}/api/v3/mcp/models", params={"lang": lang})
    resp.raise_for_status()
    data = resp.json()
    return data["data"]["models"]


def get_model_docs(model_id, lang="en"):
    """获取单个模型的详情（含价格）"""
    resp = requests.get(f"{BASE_URL}/api/v3/models/{model_id}/docs", params={"lang": lang})
    resp.raise_for_status()
    data = resp.json()
    return data.get("data")


def fetch_all_pricing(lang="en", delay=0.2):
    """批量获取所有模型的价格信息"""
    models = get_all_models(lang)
    print(f"共 {len(models)} 个模型\n")

    results = []
    for i, m in enumerate(models):
        model_id = m["id"]
        print(f"[{i+1}/{len(models)}] {model_id} ... ", end="", flush=True)

        try:
            docs = get_model_docs(model_id, lang)
            pricing = docs.get("pricing") if docs else None

            results.append({
                "id": model_id,
                "name": m["name"],
                "category": m["category"],
                "task_type": m["task_type"],
                "pricing": pricing,
            })

            if pricing:
                print(f"{pricing['price_description']}")
            else:
                print("无价格信息")

        except Exception as e:
            print(f"失败: {e}")
            results.append({
                "id": model_id,
                "name": m["name"],
                "category": m["category"],
                "task_type": m["task_type"],
                "pricing": None,
                "error": str(e),
            })

        time.sleep(delay)

    return results


def print_pricing_table(results):
    """打印价格汇总表"""
    print(f"\n{'='*90}")
    print(f"{'模型ID':<50} {'分类':<8} {'基础价格':<12} {'计价方式'}")
    print(f"{'='*90}")

    for r in results:
        p = r.get("pricing")
        if p:
            price_str = f"{p['base_price']} {p.get('price_unit', 'credits')}"
            print(f"{r['id']:<50} {r['category']:<8} {price_str:<12} {p['price_type']}")
        else:
            print(f"{r['id']:<50} {r['category']:<8} {'N/A':<12} -")


if __name__ == "__main__":
    results = fetch_all_pricing()
    print_pricing_table(results)

    with open("model_pricing.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n价格数据已保存到 model_pricing.json")
```

### 按分类筛选模型价格

```python
def get_pricing_by_category(category):
    """获取指定分类的模型价格（image/video/audio/text）"""
    models = get_all_models()
    filtered = [m for m in models if m["category"] == category]

    for m in filtered:
        docs = get_model_docs(m["id"])
        pricing = docs.get("pricing") if docs else None
        if pricing:
            print(f"{m['name']:<40} {pricing['price_description']}")
        time.sleep(0.2)

# 示例：只看视频模型价格
get_pricing_by_category("video")
```

### 查询单个模型的完整信息

```python
def show_model_detail(model_id):
    """展示单个模型的完整详情"""
    docs = get_model_docs(model_id)
    if not docs:
        print("模型不存在")
        return

    print(f"模型: {docs['name']} ({docs['id']})")
    print(f"分类: {docs['category']} / {docs['task_type']}")
    print(f"描述: {docs['description']}")

    pricing = docs.get("pricing")
    if pricing:
        print(f"\n定价:")
        print(f"  基础价格: {pricing['base_price']} {pricing.get('price_unit', 'credits')}")
        print(f"  计价方式: {pricing['price_type']}")
        print(f"  说明: {pricing['price_description']}")
        if pricing.get("price_factors"):
            print(f"  影响因素: {', '.join(pricing['price_factors'])}")
        if pricing.get("examples"):
            print(f"  价格示例:")
            for ex in pricing["examples"]:
                print(f"    - {ex['description']}: {ex['price']} 积分")

    schema = docs.get("params_schema", {})
    props = schema.get("properties", {})
    required = schema.get("required", [])
    if props:
        print(f"\n参数:")
        for name, spec in props.items():
            req_mark = " *" if name in required else ""
            desc = spec.get("description", "")
            default = f" (默认: {spec['default']})" if "default" in spec else ""
            enum = f" 可选: {spec['enum']}" if "enum" in spec else ""
            print(f"  {name}{req_mark}: {desc}{default}{enum}")

# 示例
show_model_detail("jimeng-5.0")
```

---

## 接口对照速查

| 操作 | 方法 | 路径 | 鉴权 |
|------|------|------|------|
| 获取模型列表 | GET | `/api/v3/mcp/models?lang=en` | 无需 |
| 获取模型详情(含价格) | GET | `/api/v3/models/{model_id}/docs?lang=en` | 无需 |
| 提交生成任务 | POST | `/api/v3/tasks/create` | Bearer Token |
| 查询任务结果 | POST | `/api/v3/tasks/query` | Bearer Token |

## 模型分类一览

| category | task_type | 说明 | 示例模型 |
|----------|-----------|------|----------|
| image | t2i | 文生图 | Jimeng 5.0, Seedream 4.5, Flux 2, Nano Banana |
| image | i2i | 图生图/编辑 | Seedream 4.5 Edit, Jimeng Agent |
| image | i2t | 图像理解 | LLM Image Understanding |
| video | t2v | 文生视频 | Sora 2, Seedance, Kling, Hailuo, Veo 3.1 |
| video | i2v | 图生视频 | Sora 2 I2V, Seedance I2V, Kling I2V |
| video | v2v | 视频到视频 | Sora 2 Remix, Kling Motion Control, Dreamactor |
| video | v2t | 视频理解 | LLM Video Understanding |
| audio | t2a | 文生音频/TTS | Hailuo TTS, Voice Clone, Music Gen |
| audio | stt | 语音转文 | ElevenLabs Scribe V2 |
| text | chat | 对话/LLM | GPT-5, Claude 4.6, Gemini 3.1, DeepSeek V3.1 |

---

## 注意事项

1. **模型列表是公开接口**，无需 API Key；提交任务需要 `Authorization: Bearer sk-xxx`
2. **含 `/` 的模型 ID** 直接拼在 URL 路径中即可，服务端会正确解析
3. **LLM 文本模型**的 `stats` 字段为 `null`，因为走独立计费通道
4. **`pricing` 可能为 null**，部分新模型或文本模型定价信息可能暂未返回
5. **请控制请求频率**，建议每次请求间隔 200ms 以上，避免触发限流

---

**说明（龙虾仓库）**：预扣与事后结算以本文所述 `GET .../docs` 中的 `pricing` 为准，实现见 `backend/app/services/sutui_pricing.py`、`sutui_billing_gate.py`；`lang` 参数代码中默认 `zh`。
