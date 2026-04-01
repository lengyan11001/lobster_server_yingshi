#!/usr/bin/env bash
cd /root/lobster_server || { echo "no /root/lobster_server"; exit 1; }
echo "=== PORT 8000 ==="
ss -tlnp 2>/dev/null | grep 8000 || true
echo "=== PS backend ==="
ps aux | grep -E 'uvicorn|backend\.run' | grep -v grep || true
echo "=== tail backend.log ==="
tail -80 backend.log 2>/dev/null || true
echo "=== curl / ==="
curl -sS -m 5 -o /dev/null -w 'root %{http_code}\n' http://127.0.0.1:8000/ || echo curl_root_fail
echo "=== curl manifest ==="
curl -sS -m 5 -o /dev/null -w 'manifest %{http_code}\n' http://127.0.0.1:8000/client/client-code/manifest.json || echo curl_man_fail
echo "=== nginx ==="
ls /etc/nginx/sites-enabled 2>/dev/null || true
grep -r "51ins" /etc/nginx 2>/dev/null | head -20 || true
