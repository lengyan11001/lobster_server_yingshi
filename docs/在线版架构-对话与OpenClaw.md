# 在线版架构约定：lobster_server 与 OpenClaw

> **目的**：明确 **lobster_server（Linux 公网机）** 的职责边界，避免部署或改代码时又在 **服务器上启动 OpenClaw**，或假定 `OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789` 在服务器上可用。

## 原则（必须遵守）

1. **生产环境在线部署：不在服务器上运行 OpenClaw Gateway**  
   服务器通常 **没有** `nodejs/openclaw.mjs`；即使用户误配，也不应把「用户对话」绑到服务器本机 `18789`。

2. **服务器职责（与 lobster_online 产品对齐）**  
   - **相对「仅本机后端」，在线版多出来且必须走 server 的**：注册登录、JWT、用户与积分、支付回调、计费与鉴权相关 API。  
   - **速推**：与现网一致，**统一在服务器侧**处理（`SUTUI_SERVER_TOKEN(S)`、本机 MCP 8001 调上游等），不在此文档展开实现细节。  
   - **无公网 IP**：本机客户端依赖 server 作 **公网入口与消息转发**（如微信/企微 callback、pending、submit-reply 等）。  
   - **不提供** 用户侧 OpenClaw 网关（除非未来单独产品化「云端网关」，需另文档）。

3. **启动行为**  
   - `create_app._auto_start_openclaw`：若检测不到 `node + openclaw.mjs`，应 **仅 INFO 跳过**（已实现）。  
   - 可在 `.env` 中设置 **`OPENCLAW_AUTOSTART=false`**，明确关闭探测。  
   - **禁止** 为「让在线用户用上 OpenClaw」而在服务器上安装一套 OpenClaw 作为默认方案；在线用户的 OpenClaw 在 **lobster_online 本机**。

## 对话请求应打到哪里

- **在线产品目标形态**：用户浏览器 → **本机 lobster_online** `/chat`、`/chat/stream` → 本机 OpenClaw + 本机 MCP。  
- **lobster_server** 上的 `chat` 路由：仅在不走上述形态时使用；**不应**作为在线客户端的默认对话入口（否则与「本机 OpenClaw」冲突）。

## 配置提示

- `OPENCLAW_GATEWAY_URL` / `OPENCLAW_GATEWAY_TOKEN`：在 **仅 API 的 server** 上通常 **无需** 指向真实网关；若填写 `127.0.0.1:18789` 仅表示「若误在 server 启了网关」的本地调试，**非在线用户路径**。  
- 详见项目根 `.env.example` 中 `OPENCLAW_AUTOSTART` 说明。

## 相关文档

- 客户端侧总览：`lobster_online/docs/架构说明_server与本地职责.md`  
- 对话与 OpenClaw：`lobster_online/docs/在线版架构-对话与OpenClaw.md`  
- Cursor 规则：`.cursor/rules/online-chat-openclaw-local.mdc`
