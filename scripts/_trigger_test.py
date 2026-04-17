"""Trigger a test request with a real user JWT to verify direct routing."""
import paramiko, time, json

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)

def run(cmd, timeout=60):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    return stdout.read().decode("utf-8", errors="replace").strip()

# Generate a test JWT for an existing user
print("=== Generate test JWT ===")
jwt = run("""cd /opt/lobster-server && .venv/bin/python3 -c '
import os; os.environ.setdefault("DATABASE_URL","sqlite:///./lobster.db")
from backend.app.core.config import settings
from jose import jwt as jwtlib
from datetime import datetime, timedelta
token = jwtlib.encode({"sub": "4", "exp": datetime.utcnow() + timedelta(hours=1)}, settings.secret_key, algorithm="HS256")
print(token)
'""")
print("JWT:", jwt[:30] + "..." if jwt else "FAILED")

if not jwt or "Error" in jwt or "Traceback" in jwt:
    print("Failed to generate JWT")
    print(jwt[:500])
    ssh.close()
    exit(1)

# Send test request
print("\n=== Send test chat request ===")
t0 = time.time()
body = json.dumps({"model":"deepseek-chat","messages":[{"role":"user","content":"say hi in one word"}],"stream":False})
out = run(
    "curl -s -w '\\nHTTP:%%{http_code} TIME:%%{time_total}s' "
    "-X POST http://127.0.0.1:8000/api/sutui-chat/completions "
    "-H 'Content-Type: application/json' "
    "-H 'Authorization: Bearer %s' "
    "--max-time 40 "
    "-d '%s'" % (jwt, body.replace("'", "'\\''")),
    timeout=50
)
elapsed = time.time() - t0
lines = out.split("\n")
timing = [l for l in lines if "HTTP:" in l]
body_lines = [l for l in lines if "HTTP:" not in l]
try:
    d = json.loads("\n".join(body_lines))
    usage = d.get("usage", {})
    ch = d.get("choices", [{}])[0]
    err = d.get("error")
    if err:
        print("ERROR:", err)
    else:
        print("model=%s prompt=%s compl=%s finish=%s" % (
            d.get("model","?"),
            usage.get("prompt_tokens","?"),
            usage.get("completion_tokens","?"),
            ch.get("finish_reason","?"),
        ))
        print("cache_hit=%s cache_miss=%s" % (
            usage.get("prompt_cache_hit_tokens", "N/A"),
            usage.get("prompt_cache_miss_tokens", "N/A"),
        ))
    print(timing[0] if timing else "")
except:
    print("RAW:", "\n".join(body_lines)[:300])
    print(timing[0] if timing else "")

# Check routing logs
print("\n=== Routing logs ===")
time.sleep(1)
out = run("tail -50 /opt/lobster-server/logs/app.log | grep -E 'attempts=|route|direct|roundtrip' | tail -10")
print(out if out else "(none)")

# Check billing logs
print("\n=== Billing logs ===")
out = run("tail -50 /opt/lobster-server/logs/app.log | grep -E 'billing_src|direct_official|deduct' | tail -5")
print(out if out else "(none)")

ssh.close()
print("\nDone (%.1fs)" % elapsed)
