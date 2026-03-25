# 行为对齐说明：为什么是改 Server 而不是乱改本机包

## 约定

- **本工作区无**独立 **`lobster/`** 目录；**产品行为与 prompt 约定**以 **`lobster_online`** 本机后端实现（如 `backend/app/api/chat.py`）为**对照基准**之一。
- **在线版云端后端**：`lobster-server/`，部署在云上，对外提供注册登录、积分、鉴权等中心能力。

当「在线版行为要和本机 `lobster_online` 一致」时，**在 server 侧补齐**；**不要**虚构或依赖不存在的 `lobster/` 路径。

## 为什么是 Server 的改动？

1. **本机包是体验基准**  
   `lobster_online` 里的系统提示、发布约束、素材指代、图生视频注入方式等已按产品约定打磨。与线上一致时，以该实现为对照。

2. **在线用户打到的是 lobster_server**  
   要让浏览器连 server 时的行为与 `lobster_online` 本机一致，就在 **lobster-server** 的对应路由中做等价补充（尤其是历史上曾在 server 侧承载对话逻辑时）。

3. **谁提供 API，谁承担对齐**  
   与账号、计费、鉴权相关的请求打到 **lobster_server** 时，行为一致性由 server 实现负责；本机 `lobster_online` 侧继续维护 OpenClaw、MCP、素材等（见 `lobster_online/docs/架构说明_server与本地职责.md`）。

## 实际操作

- 对照 **`lobster_online/backend/app/api/chat.py`** 中的系统提示、`_build_user_content_with_attachments` 等，在 **lobster-server** 的 `backend/app/api/chat.py` 中做同样或等价的补充与修改（以你们线上是否仍走 server chat 为准）。
- **不修改**不存在的 `lobster/` 路径；前端与文案在 **`lobster_online/`** 维护。
