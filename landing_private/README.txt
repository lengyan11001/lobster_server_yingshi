INSclaw 安装包私有目录（不公开访问）
====================================

⚠️ 此目录刻意放在 landing/ 之外，避免被 /landing 的 StaticFiles 暴露给公网静态请求。
   下载只能通过 /api/landing/download?token=xxx 接口（携带付款后生成的 token）转发。

请把以下文件放到本目录（文件名严格一致）：

  INSclaw-Setup-Windows-x64.zip       完整安装包（含 Chromium / Node / Python）

如需扩展商品（如轻量包），到 backend/app/api/landing_pay.py 的 PRODUCTS 里加一条：

  PRODUCTS = {
    "insclaw_full": { ... },
    "insclaw_slim": {
        "name": "INSclaw · 轻量包",
        "price_fen": 4900,
        "filename": "INSclaw-Slim-Windows-x64.zip",
        "download_filename": "INSclaw-Slim-Windows-x64.zip",
        "description": "已装 Python/Node 的开发者用，约 50MB",
    },
  }
