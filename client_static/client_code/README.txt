在线版客户端「纯代码包」热更新（静态文件）

与 lobster_online 启动时 scripts/check_client_code_update.py 配合（start.bat / start_online.bat 会执行）：
  - .env 配置 CLIENT_CODE_MANIFEST_URL=https://<API 域名>/client/client-code/manifest.json（完整包 install 会从 .env.example 带上此项）
  - 满足任一即拉包覆盖：① manifest.build 大于本机 build；② build 相同但 manifest.version 高于本机（如本机 1.0.0、清单写 1.0.1）
  - 须提供有效 bundle_url、sha256；paths 可省略（用客户端默认列表）

访问路径（部署 lobster-server 后）：
  GET https://<域名>/client/client-code/manifest.json
  GET https://<域名>/client/client-code/bundles/<你的包>.zip

发新版：
  1. 推荐：在 lobster_online 根目录执行
       python scripts/pack_client_code_ota.py --out <本仓库>/client_static/client_code/bundles/lobster_online_code_X.Y.Z.zip
     （与客户端 check_client_code_update 默认 paths 一致；勿含 python/、deps/、nodejs 可执行文件。）
     或 pack_code.sh（体积更大，含 wheels/docs 等，且 zip 内若含 deps/ 则不会被热更新写入）。
  2. 脚本会打印 sha256；更新 manifest.json 的 version、build、bundle_url、sha256；paths 可省略。
  3. 当前线上示例：bundles/lobster_online_code_1.0.1.zip + manifest.json 中 version=1.0.1、build=0（仅靠 semver 高于本机 1.0.0 即可触发更新）。

旧版「仅技能包」路径 /client/skill-bundle/ 仍保留，但在线客户端启动脚本已改为只查本 manifest。
