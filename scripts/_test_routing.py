"""Test that direct routing works by watching server logs."""
import paramiko, time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)

def run(cmd, timeout=60):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    return stdout.read().decode("utf-8", errors="replace").strip()

# Mark current log position
run("wc -l /opt/lobster-server/logs/app.log | awk '{print $1}' > /tmp/_log_start")

# Trigger a test request via curl to the server itself
print("=== Sending test request ===")
# Get a valid token first
token = run("cd /opt/lobster-server && grep SUTUI_SERVER_TOKENS_BIHUO .env | head -1 | cut -d= -f2").strip()
print("Token:", token[:12] if token else "NONE")

# Make a real chat request that should trigger direct DeepSeek routing
out = run("""curl -s -w '\\nHTTP:%{http_code} TIME:%{time_total}s' \
    -X POST http://127.0.0.1:8000/api/sutui-chat/completions \
    -H 'Content-Type: application/json' \
    -H 'Authorization: Bearer test-internal' \
    --max-time 45 \
    -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"say hi"}],"stream":false}'""", timeout=50)
lines = out.split("\n")
for l in lines[-3:]:
    print(" ", l[:200])

print("\n=== Check logs for routing info ===")
time.sleep(2)
start = run("cat /tmp/_log_start").strip()
if start:
    out = run("tail -n +%s /opt/lobster-server/logs/app.log | grep -iE 'attempts=|route=|direct|roundtrip|trace_id=.*enter' | tail -20" % start)
else:
    out = run("tail -30 /opt/lobster-server/logs/app.log | grep -iE 'attempts=|route=|direct|roundtrip'")
print(out if out else "(no routing logs)")

ssh.close()
print("\nDone")
