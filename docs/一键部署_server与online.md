# 一键部署：lobster-server + lobster_online

## 目标

改代码后由 **Agent 自动**完成：提交并推送 server 仓库、SSH 更新服务器、同步 **online 静态**（`lobster_online/static`），**不要求用户手敲命令**。

## 本机配置（一次性）

在 `lobster-server/` 创建 **`.env.deploy`**（已 gitignore，勿提交），可参考 **`.env.deploy.example`**：

| 变量 | 说明 |
|------|------|
| `LOBSTER_DEPLOY_HOST` | 如 `root@47.120.39.220` |
| `LOBSTER_DEPLOY_SSH_KEY` | SSH 私钥路径（可读） |
| `LOBSTER_DEPLOY_REMOTE_DIR` | 服务器上 server 目录，默认 `/root/lobster_server` |
| `LOBSTER_ONLINE_REMOTE_DIR` | 服务器上 **lobster_online 根目录**（其下为 `static/`），如 `/root/lobster_online` |

服务器上需已存在 `$LOBSTER_ONLINE_REMOTE_DIR`（可先 `mkdir -p /root/lobster_online/static`）。

## 脚本

- **仅 server**：`bash scripts/deploy_from_local.sh`（git pull + 重启）
- **server + online 静态**：`bash scripts/deploy_full_from_local.sh`（先执行上一项，再 `rsync` 本地 `lobster_online/static/` → 远端 `.../static/`）

## Agent 约定

见仓库根 `.cursor/rules/`：`server-ssh-operations.mdc`、`auto-update-restart-after-changes.mdc`。
