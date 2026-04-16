# Lobster 能力调用 API — 技能开发者文档

> 本文档面向**技能开发者**：你正在开发一个前端技能（如爆款TVC、商品详情页等），需要调用服务器的 AI 生成能力（图片、视频、语音、LLM 对话等），并走统一积分计费。

---

## 1. 认证

所有接口需要 **JWT Bearer Token**（用户登录后获取）。

```
Authorization: Bearer <token>
```

如果技能处于付费解锁体系，调用方还可携带安装槽 ID：

```
X-Installation-Id: <installation_id>
```

---

## 2. 调用流程

```
┌────────────────────────────────────────────────┐
│  1. 估价（可选）      POST /capabilities/pre-deduct  │
│     dry_run: true → 返回预计消耗积分，不扣费         │
├────────────────────────────────────────────────┤
│  2. 生成             MCP invoke_capability           │
│     系统自动处理：预扣 → 调用模型 → 结算              │
│     返回生成结果（图片URL/视频URL/文本等）            │
├────────────────────────────────────────────────┤
│  3. 查询结果（异步任务） MCP invoke_capability        │
│     capability_id: task.get_result                   │
│     传入 task_id 轮询直到完成                        │
└────────────────────────────────────────────────┘
```

> **关键**：技能开发者**不需要**手动调用 pre-deduct / record-call / refund。MCP `invoke_capability` 内部会自动完成整个计费链路。你只需关注**估价展示**和**调用生成**两件事。

---

## 3. 可用能力列表

### 3.1 查询可用能力

```
GET /capabilities/available
Authorization: Bearer <token>
```

返回示例：

```json
{
  "capabilities": [
    {
      "capability_id": "image.generate",
      "description": "生成图片（文生图/图生图）",
      "arg_schema": { ... },
      "unit_credits": 0
    },
    ...
  ]
}
```

### 3.2 核心能力一览

| capability_id | 用途 | 必要参数 | 可选参数 |
|---------------|------|----------|----------|
| `image.generate` | 文生图 / 图生图 | `prompt` | `model`, `image_url`, `image_size` |
| `video.generate` | 文生视频 / 图生视频 | `model`, `prompt` | `image_url`, `aspect_ratio`, `duration` |
| `task.get_result` | 查询异步任务结果 | `task_id` | — |
| `image.understand` | 图片理解（图→文） | `prompt` | `image_url`, `image_urls`, `model` |
| `video.understand` | 视频理解（视频→文） | `prompt` | `video_url`, `video_urls`, `model` |
| `comfly.chat` | LLM 对话补全 | `model`, `messages` | `temperature`, `max_tokens` |

#### 图片生成 — 常用模型

| model | 说明 |
|-------|------|
| `fal-ai/flux-2/flash` | Flux 2 Flash，快速文生图 |
| `jimeng-4.5` | 即梦 4.5，中文友好 |
| `seedream-4.5` | Seedream 4.5 |
| `fal-ai/nano-banana-pro` | Nano Banana Pro |

#### 视频生成 — 常用模型

| model | 说明 |
|-------|------|
| `st-ai/super-seed2` | Seedance，文生视频 |
| `wan/v2.6/image-to-video` | Wan 2.6，图生视频 |
| `fal-ai/kling-video/o3/pro/image-to-video` | Kling 图生视频 |
| `fal-ai/minimax/video-01-live/image-to-video` | Hailuo 图生视频 |
| `veo3.1-fast` | Veo 3.1（Comfly 路由） |

#### LLM 对话 — 常用模型

| model | 说明 |
|-------|------|
| `veo3.1-fast` | Comfly 模型 |
| `google/gemini-3-pro-preview` | Gemini 3 Pro（图片/视频理解默认） |

---

## 4. 估价（前端展示费用确认）

在调用生成前，可先查询本次大约消耗多少积分：

```
POST /capabilities/pre-deduct
Authorization: Bearer <token>
Content-Type: application/json

{
  "capability_id": "video.generate",
  "model": "wan/v2.6/image-to-video",
  "dry_run": true
}
```

返回：

```json
{
  "credits_charged": 15.0,
  "dry_run": true,
  "model": "wan/v2.6/image-to-video"
}
```

> `credits_charged` 为**预估值**，实际消耗以生成完成后结算为准。用户积分不足时返回 `402`。

---

## 5. 调用生成（MCP invoke_capability）

### 5.1 文生图

```json
{
  "capability_id": "image.generate",
  "payload": {
    "prompt": "一只穿着宇航服的猫咪在月球上散步，电影级画质",
    "model": "jimeng-4.5",
    "image_size": "landscape_16_9"
  }
}
```

返回：

```json
{
  "capability_id": "image.generate",
  "result": {
    "images": [{ "url": "https://cdn.example.com/xxx.png" }]
  },
  "credits_used": 5.0
}
```

### 5.2 图生视频

```json
{
  "capability_id": "video.generate",
  "payload": {
    "prompt": "镜头缓缓推进，人物微笑转头",
    "model": "wan/v2.6/image-to-video",
    "image_url": "https://example.com/photo.jpg",
    "duration": 5
  }
}
```

视频生成为**异步任务**，返回：

```json
{
  "capability_id": "video.generate",
  "result": {
    "task_id": "abc123..."
  }
}
```

### 5.3 查询异步结果

```json
{
  "capability_id": "task.get_result",
  "payload": {
    "task_id": "abc123..."
  }
}
```

任务进行中返回：

```json
{
  "result": { "status": "processing" }
}
```

任务完成返回：

```json
{
  "result": {
    "status": "completed",
    "video": { "url": "https://cdn.example.com/xxx.mp4" }
  }
}
```

> 服务端会自动长轮询（最多等 30 分钟），每 60 秒检查一次。通常不需重复调用。

### 5.4 图片理解

```json
{
  "capability_id": "image.understand",
  "payload": {
    "prompt": "详细描述这张图片中的所有元素",
    "image_url": "https://example.com/photo.jpg"
  }
}
```

### 5.5 LLM 对话（Comfly）

```json
{
  "capability_id": "comfly.chat",
  "payload": {
    "model": "veo3.1-fast",
    "messages": [
      { "role": "system", "content": "你是一个电商文案专家" },
      { "role": "user", "content": "为一款保温杯写 5 条卖点文案" }
    ],
    "temperature": 0.8
  }
}
```

### 5.6 语音合成

```json
{
  "capability_id": "sutui.speak",
  "payload": {
    "action": "synthesize",
    "text": "大家好，欢迎来到我们的直播间",
    "voice_id": "xxx",
    "model": "speech-2.8-hd"
  }
}
```

---

## 6. 前端 JS 调用示例

技能前端代码中调用 MCP 能力的标准方式：

```javascript
// 通用 MCP 工具调用封装
async function invokeCapability(capabilityId, payload) {
  const resp = await fetch('/mcp', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + getToken(),
    },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: Date.now(),
      method: 'tools/call',
      params: {
        name: 'invoke_capability',
        arguments: {
          capability_id: capabilityId,
          payload: payload,
        },
      },
    }),
  });
  const data = await resp.json();
  return data.result;
}

// ── 使用示例 ──

// 1. 文生图
const imgResult = await invokeCapability('image.generate', {
  prompt: '赛博朋克风格的城市夜景',
  model: 'jimeng-4.5',
});
const imageUrl = imgResult.content[0].text; // JSON 字符串，需 parse

// 2. 生成视频（异步）
const videoTask = await invokeCapability('video.generate', {
  prompt: '镜头缓慢向前推进',
  model: 'st-ai/super-seed2',
  image_url: imageUrl,
});
const taskId = JSON.parse(videoTask.content[0].text).result.task_id;

// 3. 等待视频完成
const videoResult = await invokeCapability('task.get_result', {
  task_id: taskId,
});
```

### 估价（费用确认弹窗）

```javascript
async function estimateCost(capabilityId, model) {
  const resp = await fetch('/capabilities/pre-deduct', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + getToken(),
    },
    body: JSON.stringify({
      capability_id: capabilityId,
      model: model,
      dry_run: true,
    }),
  });
  const data = await resp.json();
  return data.credits_charged; // 预估积分消耗
}

// 使用
const cost = await estimateCost('video.generate', 'wan/v2.6/image-to-video');
if (confirm(`本次生成预计消耗 ${cost} 积分，确认？`)) {
  // 执行生成...
}
```

---

## 7. 错误码

| HTTP 状态码 | 含义 |
|-------------|------|
| `200` | 成功 |
| `400` | 参数错误（如缺少 model） |
| `401` | 未登录或 token 过期 |
| `402` | 积分不足 |
| `403` | 能力未解锁（付费技能需先购买） |
| `502` | 上游模型调用失败 |

积分不足时返回：

```json
{
  "detail": "积分不足：本次需 15 积分，当前余额 3。请先充值。"
}
```

---

## 8. 计费说明

- **积分消耗** = 模型采购价 × 倍率（默认 3 倍）
- 系统自动比价：同时支持多个上游供应商，优先选择价格更低的通道
- **预扣制**：生成前冻结估价积分，生成完成后按实际消耗结算（多退少补）
- 生成失败时积分**自动退回**
- `task.get_result`（查询结果）和 `image.understand`（图片理解）**不额外收费**

---

## 9. 开发建议

1. **先估价再生成**：调用 `pre-deduct` + `dry_run: true` 获取预估费用，展示给用户确认后再调用
2. **异步任务轮询**：视频生成通常需要 30-120 秒，使用 `task.get_result` 轮询，服务端会自动等待
3. **错误处理**：捕获 `402`（积分不足）并引导用户充值
4. **模型选择**：同类型模型价格和效果差异大，建议在技能中预设推荐模型，而非让用户自选
