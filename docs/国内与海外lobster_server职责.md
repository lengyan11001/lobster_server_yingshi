# 国内与海外 lobster_server 职责

同仓库 `main` 可部署多台。**在线版前端**：

| 变量 | 典型地址 | 职责 |
|------|----------|------|
| `API_BASE` | 国内 `http://42.194.209.150`（备案后切 `https://bhzn.top`） | 登录、积分、支付、鉴权、技能商店等 |
| `MESSENGER_API_BASE` / `TWILIO_API_BASE` | 海外 `http://43.162.111.36`（43.162.111.36） | Messenger、Twilio；Webhook 须公网可达 Meta/Twilio |

本地执行：`git push` 后 `bash scripts/deploy_from_local.sh`。若 `.env.deploy` 含 `LOBSTER_DEPLOY_HOST_OVERSEAS`，会再拉取并重启海外机。

**海外 SSH**：与 **`docs/云服务器部署说明.md`** 一致——本机 **`deploy_from_local.sh`**，密钥见 `.env.deploy`；海外示例 **`ubuntu@43.162.111.36`**、`/opt/lobster-server`（勿提交密码到 git）。

详见 `docs/Messenger配置说明.md`。
