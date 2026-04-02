# lobster_server · 部署（开发机）

架构见 `docs/云服务器部署说明.md`、`docs/在线版架构-对话与OpenClaw.md`。

## 一键发布（推荐）

在 **`lobster-server`** 仓库根目录，已配置好 **`.env.deploy`**（见 `.env.deploy.example`）后：

```bash
chmod +x scripts/deploy_publish.sh scripts/deploy_server.sh scripts/deploy_from_local.sh
bash scripts/deploy_publish.sh
```

- 若有未提交改动：自动 `git commit` → `git push origin main` → SSH 远端 `git pull` 并重启 Backend+MCP。  
- 若无改动：直接 `push`（若已是最新则无推送）→ 仍执行远端更新脚本。

部署链路以 `docs/云服务器部署说明.md`「日常更新」为准：**仅 Git**；勿用 SCP 顶替。Windows 若 `git push` 报 `Permission denied (publickey)`，见 `docs/运维备忘-SSH密钥与部署.md`「GitHub」。

仅推送**已提交**代码、不要自动提交时：

```bash
bash scripts/deploy_server.sh
```

## 首次一次

`cp .env.deploy.example .env.deploy`，填写 `LOBSTER_DEPLOY_HOST`、`LOBSTER_DEPLOY_SSH_KEY`、`LOBSTER_DEPLOY_REMOTE_DIR`（如 `/root/lobster_server`）。

**海外第二台**（与 `lobster-server.icu` / Messenger·Twilio 同机）：在 `.env.deploy` 增加 `LOBSTER_DEPLOY_HOST_OVERSEAS`、`LOBSTER_DEPLOY_REMOTE_DIR_OVERSEAS`；SSH 与大陆相同（默认复用 `LOBSTER_DEPLOY_SSH_KEY`，海外用户 `~/.ssh/authorized_keys` 须含对应公钥）。流程同 `docs/云服务器部署说明.md` §「日常更新代码并重启」。

## 与 lobster_online

**不**把 `lobster_online` 部署到 ECS；用户本机解压代码包运行。Server 提供账号、鉴权、积分、速推、`upload-temp` 等。
