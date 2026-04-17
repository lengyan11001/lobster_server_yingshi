"""Test xskill with realistic full payload from server."""
import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)

def run(cmd, timeout=120):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if err:
        print(f"  STDERR: {err[:500]}")
    return out

# Run realistic test on server using its own Python env
script = r'''
import time, json, httpx, os, sys
sys.path.insert(0, "/opt/lobster-server")
os.chdir("/opt/lobster-server")
from dotenv import load_dotenv
load_dotenv()

token = (os.environ.get("SUTUI_SERVER_TOKENS") or os.environ.get("SUTUI_SERVER_TOKEN") or "").split(",")[0].strip()
if not token:
    print("NO TOKEN")
    sys.exit(1)

# Fetch tools from local MCP
try:
    r = httpx.post("http://127.0.0.1:8001/", json={"jsonrpc":"2.0","id":"t","method":"tools/list","params":{}}, timeout=5)
    tools = r.json().get("result",{}).get("tools",[])
except Exception as e:
    print(f"MCP error: {e}")
    tools = []

oai_tools = [{"type":"function","function":{"name":t["name"],"description":t.get("description",""),"parameters":t.get("inputSchema",{"type":"object","properties":{}})}} for t in tools]
print(f"MCP tools: {len(tools)}, names: {[t['name'] for t in tools]}")

# Build realistic body (similar to what client sends)
body = {
    "model":"deepseek-chat",
    "messages":[
        {"role":"system","content":"you are lobster assistant with tools"},
        {"role":"user","content":"hi"},
        {"role":"assistant","content":"hello! how can I help?"},
        {"role":"user","content":"list my capabilities"},
        {"role":"assistant","content":"let me check..."},
        {"role":"user","content":"generate a tiger image"},
    ],
    "tools":oai_tools,
    "tool_choice":"auto",
    "stream":False
}
body_json = json.dumps(body, ensure_ascii=False)
print(f"Payload: {len(body_json)} chars, ~{len(body_json)//3} tokens est")

# Test 3 times
for i in range(3):
    t0 = time.time()
    try:
        client = httpx.Client(timeout=45)
        resp = client.post(
            "https://api.xskill.ai/v1/chat/completions",
            json=body,
            headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
        )
        elapsed = time.time()-t0
        d = resp.json()
        pt = d.get("usage",{}).get("prompt_tokens","?")
        ct = d.get("usage",{}).get("completion_tokens","?")
        fr = d.get("choices",[{}])[0].get("finish_reason","?")
        tc = d.get("choices",[{}])[0].get("message",{}).get("tool_calls")
        err = d.get("error")
        if err:
            print(f"  [{i+1}] ERROR: {err} elapsed={elapsed:.1f}s")
        else:
            print(f"  [{i+1}] prompt={pt} completion={ct} finish={fr} tool_calls={bool(tc)} elapsed={elapsed:.1f}s")
        client.close()
    except Exception as e:
        elapsed = time.time()-t0
        print(f"  [{i+1}] EXCEPTION: {type(e).__name__}: {e} elapsed={elapsed:.1f}s")

# Also test claude
print("\\n--- claude-opus-4-6 with same tools ---")
body["model"] = "claude-opus-4-6"
t0 = time.time()
try:
    client = httpx.Client(timeout=45)
    resp = client.post(
        "https://api.xskill.ai/v1/chat/completions",
        json=body,
        headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    )
    elapsed = time.time()-t0
    d = resp.json()
    pt = d.get("usage",{}).get("prompt_tokens","?")
    fr = d.get("choices",[{}])[0].get("finish_reason","?")
    tc = d.get("choices",[{}])[0].get("message",{}).get("tool_calls")
    err = d.get("error")
    if err:
        print(f"  ERROR: {err} elapsed={elapsed:.1f}s")
    else:
        print(f"  prompt={pt} finish={fr} tool_calls={bool(tc)} elapsed={elapsed:.1f}s")
    client.close()
except Exception as e:
    elapsed = time.time()-t0
    print(f"  EXCEPTION: {type(e).__name__}: {e} elapsed={elapsed:.1f}s")
'''

print("Running realistic payload tests on server...\n")
out = run(f'cd /opt/lobster-server && .venv/bin/python3 -c """{script}"""', timeout=120)
print(out)

ssh.close()
