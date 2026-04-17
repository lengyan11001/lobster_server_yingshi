import sys, os, paramiko

host = "47.120.39.220"
user = "root"
key_path = "D:/maczhuji"
passphrase = "lengyan2"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
pkey = paramiko.RSAKey.from_private_key_file(key_path, password=passphrase)
client.connect(host, username=user, pkey=pkey, timeout=15)

cmd = """cd /root/lobster_server && .venv/bin/python3 << 'PYEOF'
import json, urllib.request

tokens = {
    "BIHUO": "sk-838e4bda7bf9555f9d9b0f95d2cdaf6604359e3ed00409c0",
    "YINGSHI": "sk-fde25920cbb19bb86ef477ce109b0f7f3a9e71f4e0a27029",
}

url = "https://api.xskill.ai/api/v3/balance"

for pool, tok in tokens.items():
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        print(f"{pool}: {json.dumps(data, ensure_ascii=False, indent=2)}")
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ''
        try:
            req2 = urllib.request.Request(f"{url}?api_key={tok}", headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            resp2 = urllib.request.urlopen(req2, timeout=15)
            data2 = json.loads(resp2.read())
            print(f"{pool} (api_key): {json.dumps(data2, ensure_ascii=False, indent=2)}")
        except Exception as e2:
            print(f"{pool}: Bearer={e.code} body={body[:200]}, api_key={e2}")
    except Exception as e:
        print(f"{pool}: failed={e}")
PYEOF
"""
stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
out = stdout.read().decode()
err = stderr.read().decode()
if out:
    print(out)
if err:
    print("STDERR:", err)
