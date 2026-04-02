在线版客户端技能热更新（静态文件）

【说明】在线客户端启动脚本已改为「纯代码包」OTA（/client/client-code/，见同目录上一级 client_code/README.txt）。
本目录仍保留，供仍配置 SKILL_BUNDLE_MANIFEST_URL 的旧包或手工拉取使用。

访问路径（部署 lobster-server 后）：
  GET https://<你的 API 域名>/client/skill-bundle/manifest.json
  GET https://<你的 API 域名>/client/skill-bundle/bundles/base_v1.zip

lobster_online 用户 .env 示例：
  SKILL_BUNDLE_MANIFEST_URL=https://<你的 API 域名>/client/skill-bundle/manifest.json

发新版：提高 manifest.json 的 build，上传新 zip，更新 sha256 与 bundle_url（若文件名变）。
