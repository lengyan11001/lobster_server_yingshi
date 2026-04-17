#!/usr/bin/env bash
# [已废弃] 旧 api.51ins.com 宝塔面板用。新服务器 bhzn.top 的 nginx 已内置相关配置。
# nginx error: upstream timed out while reading response header from upstream（默认 proxy_read_timeout 60s）
set -euo pipefail
CONF="/www/server/panel/vhost/nginx/api.51ins.com.conf"
if [[ ! -f "$CONF" ]]; then
  echo "[ERR] missing $CONF"
  exit 1
fi
if grep -q "proxy_read_timeout 300s" "$CONF" && grep -q "location = /mcp-gateway" "$CONF"; then
  echo "[OK] already patched (proxy_read_timeout 300s present)"
  exit 0
fi
cp -a "$CONF" "${CONF}.bak.$(date +%Y%m%d%H%M%S)"
python3 << 'PY'
from pathlib import Path
p = Path("/www/server/panel/vhost/nginx/api.51ins.com.conf")
text = p.read_text(encoding="utf-8")
needle = """    location = /mcp-gateway {
      proxy_pass http://127.0.0.1:8000;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
    }"""
insert = """    location = /mcp-gateway {
      proxy_pass http://127.0.0.1:8000;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_connect_timeout 60s;
      proxy_send_timeout 300s;
      proxy_read_timeout 300s;
      proxy_cache off;
      proxy_buffering off;
    }"""
if needle not in text:
    raise SystemExit("[ERR] expected mcp-gateway block not found; edit script or conf manually")
if insert in text:
    print("[OK] block already contains extended timeouts")
else:
    text = text.replace(needle, insert, 1)
    p.write_text(text, encoding="utf-8")
    print("[OK] wrote", p)
PY
nginx -t
nginx -s reload
echo "[完成] nginx 已重载"
