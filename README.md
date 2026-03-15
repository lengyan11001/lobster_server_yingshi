# 龙虾 · 服务器部署包

本目录为**仅用于云服务器部署**的代码子集，由 `lobster` 源码目录同步生成。

- **本地开发 / 打 online 版**：在 **`lobster/`** 目录进行。
- **服务器部署**：使用本目录 **`lobster-server/`**。Git 拉取后在此目录执行安装与启动即可。

## 如何更新本目录（在开发机执行）

在 `lobster` 项目根目录执行：

```bash
cd lobster
./scripts/build_server_package.sh
```

会将当前 `lobster` 中运行服务所需文件同步到同级的 `lobster-server/`（或你指定的目标目录）。

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
