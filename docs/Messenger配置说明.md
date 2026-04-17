# Messenger 多应用配置说明

## 总架构（与「全站迁海外」无关）

- **登录、注册、验证码、`/auth/me`、积分、充值、技能商店列表、企微等**：仍走**原大陆 lobster_server**（在线版里的 **`API_BASE`** / `lobster_api_base`，与过去一致）。
- **仅需要出海的少数能力**（当前为 **Messenger**：配置 CRUD + Webhook 回调）：在线版把 **`MESSENGER_API_BASE`** 单独指向**海外机**即可；**不需要**把整站用户迁到海外。
- **Webhook**：由 Meta 直连海外 URL，与浏览器无关；**发消息**也在海外进程内完成。

### 浏览器带登录态调海外 CRUD 时

大陆颁发的 JWT 要在海外校验通过，且能解析到同一用户，需同时满足：**`SECRET_KEY` 与大陆一致** + **`users` 等账号数据与大陆一致**（典型做法：**共用 MySQL**，或海外只读同步用户表；**不能**两机各一套 SQLite 却共用 JWT）。若暂不具备共用库，可在**海外** `.env` 设置 **`MESSENGER_TRUST_JWT_WITHOUT_USER=true`**（并保证 **`SECRET_KEY` 与大陆签发 JWT 时一致**），则海外库中即使没有对应 `users` 行，仍用 JWT 的 `sub` 作为 `messenger_configs.user_id`。**仅部署在海外实例；大陆实例保持默认 `false`。**

## 一、能力侧要点

- **Webhook、Graph API** 仅部署在**可访问 Meta 的海外机**（如 `43.162.111.36`）。
- **在线客户端**里 **「Messenger」** 与技能卡里 **「Facebook Messenger 客服」** 的 CRUD 请求只发往 **`MESSENGER_API_BASE`**，与 **`API_BASE`** 分离。

## 二、配置项总表（按条「应用」填写）

在在线版 **Messenger** 页「添加应用」中填写，与 Meta 开发者后台一致：

| 字段 | 说明 |
|------|------|
| **显示名称** | 本地备注，便于区分多个 Facebook 应用。 |
| **Verify Token** | 自定义强随机字符串；在 Meta → 应用 → Messenger → Webhook 的 **Verify Token** 填**完全相同**的值。 |
| **App Secret** | Meta 应用面板 **应用密钥**；用于校验 Webhook POST 的 `X-Hub-Signature-256`。 |
| **Page ID** | Facebook **公共主页数字 ID**（与订阅的 Page 一致）；用于校验回调 `entry.id`。 |
| **Page Access Token** | 带 **pages_messaging** 等权限的 Page 令牌；用于调用 Graph 发消息。 |
| **产品知识（可选）** | 附加到 AI 系统提示，与企微「产品知识」类似。 |

保存后列表中会生成 **Webhook URL**，形如：

`{PUBLIC_BASE_URL}/api/messenger/callback/{callback_path}`

在 Meta 后台 **Callback URL** 填此完整地址（**HTTPS 生产**时请将 `PUBLIC_BASE_URL` 配为 `https://你的域名`，并保证 443 反代到后端）。

## 三、Meta 控制台操作顺序（每个应用）

1. 创建/选择 **Facebook 应用**，添加 **Messenger** 产品。
2. 关联 **Facebook 公共主页**，生成 **Page Access Token**（长期 Token 按 Meta 文档续期）。
3. **Webhook**：URL = 上节完整 URL；**Verify Token** = 本系统该条配置的 Verify Token；验证并保存。
4. **订阅字段**：至少勾选 **messages**。
5. 将 **App Secret**、**Page ID**、**Page Access Token** 填入本系统对应字段并保存。

## 四、服务端环境变量（仅海外机）

| 变量 | 说明 |
|------|------|
| `PUBLIC_BASE_URL` | 与对外访问一致，如 `http://43.162.111.36:8000` 或 `http://43.162.111.36`（配好 Nginx 后）。用于拼接返回前端的 `webhook_url`。 |
| `SECRET_KEY` | 与大陆签发登录 JWT 时**一致**（使用 `MESSENGER_TRUST_JWT_WITHOUT_USER` 时必填）。 |
| `MESSENGER_TRUST_JWT_WITHOUT_USER` | 海外建议 `true`（大陆登录 + 海外无用户行时）；大陆 **`false`**。 |

**不再**使用全局 `MESSENGER_PAGE_ACCESS_TOKEN` 作为业务主路径；多应用均以数据库 `messenger_configs` 为准。

## 五、验证步骤

1. `GET {MESSENGER_API_BASE}/docs` 可打开。
2. 登录后打开 **Messenger** 页，能 **列出/新增** 配置。
3. 在 Meta 后台点击 **验证 Webhook**，应成功。
4. 在 Messenger 窗口向主页发文本消息，海外日志应出现处理记录，并收到 AI 回复（需已配置对话模型或 OpenClaw，与企微通道一致）。
