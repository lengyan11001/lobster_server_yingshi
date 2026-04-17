"""Diagnose xskill.ai directly from server - get token from running env."""
import paramiko, json, time

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)

def run(cmd, timeout=180):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    return stdout.read().decode("utf-8", errors="replace").strip()

# Get token from server .env
token = run("cd /opt/lobster-server && grep -E '^SUTUI_SERVER_TOKEN' .env | head -1 | cut -d= -f2- | tr -d '\"' | tr ',' '\\n' | head -1").strip()
if not token:
    token = run("cd /opt/lobster-server && grep -E '^SUTUI_SERVER_TOKENS' .env | head -1 | cut -d= -f2- | tr -d '\"' | tr ',' '\\n' | head -1").strip()
print(f"Token: {'yes (' + token[:8] + '...)' if token else 'NOT FOUND'}")

if not token:
    print("Cannot proceed without token")
    ssh.close()
    exit(1)

# Test 1: Simple request to deepseek via xskill (no tools)
print("\n=== Test 1: deepseek-chat via xskill, NO tools, 'hi' ===")
for i in range(3):
    t0 = time.time()
    out = run(f"""curl -s -w '\\nHTTP:%{{http_code}} TIME:%{{time_total}}s TTFB:%{{time_starttransfer}}s' \
        -X POST https://api.xskill.ai/v1/chat/completions \
        -H 'Authorization: Bearer {token}' \
        -H 'Content-Type: application/json' \
        --max-time 30 \
        -d '{{"model":"deepseek-chat","messages":[{{"role":"user","content":"hi"}}],"stream":false}}'""", timeout=40)
    lines = out.strip().split("\n")
    timing = [l for l in lines if "HTTP:" in l]
    body = "\n".join([l for l in lines if "HTTP:" not in l])
    try:
        d = json.loads(body)
        pt = d.get("usage", {}).get("prompt_tokens", "?")
        ct = d.get("usage", {}).get("completion_tokens", "?")
        err = d.get("error", {})
        if err:
            print(f"  [{i+1}] ERROR: {err}")
        else:
            print(f"  [{i+1}] prompt={pt} completion={ct} {timing[0] if timing else ''}")
    except:
        print(f"  [{i+1}] {body[:200]} {timing[0] if timing else ''}")

# Test 2: claude via xskill (no tools)
print("\n=== Test 2: claude-opus-4-6 via xskill, NO tools, 'hi' ===")
for i in range(2):
    out = run(f"""curl -s -w '\\nHTTP:%{{http_code}} TIME:%{{time_total}}s TTFB:%{{time_starttransfer}}s' \
        -X POST https://api.xskill.ai/v1/chat/completions \
        -H 'Authorization: Bearer {token}' \
        -H 'Content-Type: application/json' \
        --max-time 30 \
        -d '{{"model":"claude-opus-4-6","messages":[{{"role":"user","content":"hi"}}],"stream":false}}'""", timeout=40)
    lines = out.strip().split("\n")
    timing = [l for l in lines if "HTTP:" in l]
    body = "\n".join([l for l in lines if "HTTP:" not in l])
    try:
        d = json.loads(body)
        pt = d.get("usage", {}).get("prompt_tokens", "?")
        ct = d.get("usage", {}).get("completion_tokens", "?")
        err = d.get("error", {})
        if err:
            print(f"  [{i+1}] ERROR: {err}")
        else:
            print(f"  [{i+1}] prompt={pt} completion={ct} {timing[0] if timing else ''}")
    except:
        print(f"  [{i+1}] {body[:200]} {timing[0] if timing else ''}")

# Test 3: deepseek WITH tools (real scenario, small tools set)
print("\n=== Test 3: deepseek-chat via xskill, WITH 3 tools, 'generate tiger' ===")
tools_payload = json.dumps({
    "model":"deepseek-chat",
    "messages":[{"role":"system","content":"you are a helper"},{"role":"user","content":"generate a tiger image"}],
    "tools":[
        {"type":"function","function":{"name":"generate","description":"gen image","parameters":{"type":"object","properties":{"prompt":{"type":"string"}},"required":["prompt"]}}},
        {"type":"function","function":{"name":"list","description":"list items","parameters":{"type":"object","properties":{}}}},
        {"type":"function","function":{"name":"publish","description":"publish item","parameters":{"type":"object","properties":{"id":{"type":"string"}},"required":["id"]}}}
    ],
    "tool_choice":"auto","stream":False
}).replace("'", "'\\''")
out = run(f"""curl -s -w '\\nHTTP:%{{http_code}} TIME:%{{time_total}}s TTFB:%{{time_starttransfer}}s' \
    -X POST https://api.xskill.ai/v1/chat/completions \
    -H 'Authorization: Bearer {token}' \
    -H 'Content-Type: application/json' \
    --max-time 60 \
    -d '{tools_payload}'""", timeout=70)
lines = out.strip().split("\n")
timing = [l for l in lines if "HTTP:" in l]
body_str = "\n".join([l for l in lines if "HTTP:" not in l])
try:
    d = json.loads(body_str)
    pt = d.get("usage", {}).get("prompt_tokens", "?")
    fr = d.get("choices", [{}])[0].get("finish_reason", "?")
    tc = d.get("choices", [{}])[0].get("message", {}).get("tool_calls")
    print(f"  prompt={pt} finish_reason={fr} has_tool_calls={bool(tc)} {timing[0] if timing else ''}")
except:
    print(f"  {body_str[:300]} {timing[0] if timing else ''}")

# Test 4: deepseek WITH FULL 22 tools (actual client payload size)
print("\n=== Test 4: deepseek-chat via xskill, realistic payload (fetch from running server) ===")
out = run(f"""cd /opt/lobster-server && python3 -c "
import time, json, httpx
from dotenv import load_dotenv
import os
load_dotenv()
token = (os.environ.get('SUTUI_SERVER_TOKENS') or os.environ.get('SUTUI_SERVER_TOKEN') or '').split(',')[0].strip()
# Get tools from MCP
try:
    r = httpx.post('http://127.0.0.1:8001/', json={{'jsonrpc':'2.0','id':'t','method':'tools/list','params':{{}}}}, timeout=5)
    tools = r.json().get('result',{{}}).get('tools',[])
except: tools = []
oai_tools = [{{'type':'function','function':{{'name':t['name'],'description':t.get('description',''),'parameters':t.get('inputSchema',{{'type':'object','properties':{{}}}})}}}} for t in tools]
body = {{
    'model':'deepseek-chat',
    'messages':[{{'role':'user','content':'generate a tiger image'}}],
    'tools':oai_tools,
    'tool_choice':'auto',
    'stream':False
}}
body_json = json.dumps(body, ensure_ascii=False)
print(f'payload_chars={{len(body_json)}} tools_count={{len(oai_tools)}}')
t0 = time.time()
try:
    client = httpx.Client(timeout=60)
    resp = client.post('https://api.xskill.ai/v1/chat/completions', json=body, headers={{'Authorization':f'Bearer {{token}}','Content-Type':'application/json'}})
    elapsed = time.time()-t0
    d = resp.json()
    pt = d.get('usage',{{}}).get('prompt_tokens','?')
    fr = d.get('choices',[{{}}])[0].get('finish_reason','?')
    tc = d.get('choices',[{{}}])[0].get('message',{{}}).get('tool_calls')
    err = d.get('error')
    if err:
        print(f'ERROR: {{err}} elapsed={{elapsed:.1f}}s')
    else:
        print(f'prompt={{pt}} finish_reason={{fr}} tool_calls={{bool(tc)}} elapsed={{elapsed:.1f}}s http={{resp.status_code}}')
except Exception as e:
    elapsed = time.time()-t0
    print(f'EXCEPTION: {{e}} elapsed={{elapsed:.1f}}s')
"
""", timeout=90)
print(f"  {out}")

print("\n=== Test 5: Check xskill.ai models/health ===")
out = run(f"""curl -s -w '\\nHTTP:%{{http_code}} TIME:%{{time_total}}s' \
    https://api.xskill.ai/v1/models \
    -H 'Authorization: Bearer {token}' \
    --max-time 10 | head -c 500""", timeout=15)
lines = out.strip().split("\n")
timing = [l for l in lines if "HTTP:" in l]
print(f"  Models endpoint: {timing[0] if timing else 'no timing'}")

ssh.close()
print("\nDone")
