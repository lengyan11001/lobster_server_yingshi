# 龙虾 · 服务器部署包

本目录为**仅用于云服务器部署**的代码子集，与 **`lobster_online/`** 本机完整包、**`lobster-server/`** 云端 API 分工协作。

- **本地开发 / 打 online 版**：在 **`lobster_online/`** 目录进行（本工作区**无**顶层 `lobster/` 目录）。
- **服务器部署**：使用本目录 **`lobster-server/`**。Git 拉取后在此目录执行安装与启动即可。

## 如何更新本目录（在开发机执行）

若你的仓库提供从 `lobster_online` 同步到 `lobster-server` 的脚本，在**对应仓库根目录**执行（示例；以仓库内实际脚本为准）：

```bash
# 示例：若存在 scripts/build_server_package.sh
./scripts/build_server_package.sh
```

会将运行服务所需文件同步到 **`lobster-server/`**（或你指定的目标目录）。

## 云服务器快速启动

```bash
git clone <仓库地址> ...
cd lobster-server
chmod +x scripts/*.sh
./scripts/server_install.sh
# 编辑 .env 填写 SECRET_KEY、SUTUI_SERVER_TOKEN 等
./scripts/server_start.sh
```

访问 `http://服务器IP:8000`。详细步骤与 systemd/Nginx 见 `docs/云服务器部署说明.md`。
