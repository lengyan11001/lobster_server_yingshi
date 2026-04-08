# 运维备忘：SSH、GitHub 与部署（本机 Windows）

> 私钥只存本机文件；**口令、主机、真实路径的唯一权威来源**是 **`lobster-server/.env.deploy`**（已由 `.gitignore` 忽略，**勿提交**）。  
> 本文只写步骤与占位符，**不记录任何口令或机密**，避免仓库泄露后「全链路失守」。

---

## 1. 本机必备（与脚本一致）

| 项 | 说明 |
|----|------|
| 私钥文件 | 放在你本机固定路径（例如你惯用的 `D:\maczhuji`）；路径写进 **`.env.deploy`** 的 `LOBSTER_DEPLOY_SSH_KEY`（Git Bash 可用 `/d/...`）。 |
| 私钥口令 | **只写**在 **`.env.deploy`** 的 `LOBSTER_SSH_KEY_PASSPHRASE`；`deploy_from_local.sh`、`tail_remote_logs.sh`、`ssh_run_remote.sh` 会用它做非交互 `ssh-add`。 |
| 勿把口令写进 Markdown / 提交到 Git | 历史若曾误提交，应轮换口令或限制该密钥权限，并 `git filter-repo` 等清理历史（另议）。 |

---

## 2. GitHub（git push / git fetch）

- **远端**：以 `git remote -v` 为准。
- **`%USERPROFILE%\.ssh\config`**（示例，路径改成你的私钥）：

```sshconfig
Host github.com
  HostName github.com
  User git
  IdentityFile D:/你的私钥文件
  IdentitiesOnly yes
```

- **加密私钥要先加入 ssh-agent**（否则 push 常 `Permission denied`）。PowerShell 示例：

```powershell
Get-Service ssh-agent
$env:DISPLAY = "localhost:0"
$env:SSH_ASKPASS = "$env:USERPROFILE\.ssh\你的-askpass.cmd"
$env:SSH_ASKPASS_REQUIRE = "force"
ssh-add D:\你的私钥文件
ssh-add -l
```

- **SSH_ASKPASS 小脚本**：本地单独建一个 `.cmd`，内容由你本机保管（`echo` 出口令仅给你自己用），**不要**放进仓库。

- **验证**：`cd` 到 `lobster-server`，`git fetch` / `git push`。

---

## 3. 服务器部署（仅 Git，不用 SFTP）

1. **`cp .env.deploy.example .env.deploy`**，按注释填齐 **`LOBSTER_DEPLOY_HOST`**、**`LOBSTER_DEPLOY_SSH_KEY`**、**`LOBSTER_DEPLOY_REMOTE_DIR`**；有口令则填 **`LOBSTER_SSH_KEY_PASSPHRASE`**。海外机见 `.env.deploy.example` 的 `*_OVERSEAS`。
2. 本机 **`git push origin main` 成功** 后：

```bash
cd /path/to/lobster-server
bash scripts/deploy_from_local.sh
```

3. **看远端日志**（不等价于「手动 ssh」，但同一套密钥）：`bash scripts/tail_remote_logs.sh`  
   若在 **lobster_online** 且与 **lobster-server** 同级：可运行 **`lobster_online/scripts/tail_lobster_server_logs.bat`**（转调上述脚本）。

---

## 4. 可选：Python 读远程日志

- `scripts/ssh_sample_remote_mcp_log.py` 同样只读 **`.env.deploy`**，不把机密写进代码。

---

## 5. 多业务隔离

- **龙虾**：仅用本仓库 `.env.deploy` 里的 `LOBSTER_DEPLOY_*`。  
- **其它项目**：各自 `.env` / 文档，勿混用同一备忘本。
