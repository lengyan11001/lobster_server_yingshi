import time, json, httpx, os, sys
sys.path.insert(0, "/opt/lobster-server")
os.chdir("/opt/lobster-server")

# Manually read .env
token = ""
if os.path.exists(".env"):
    for line in open(".env"):
        line = line.strip()
        if line.startswith("SUTUI_SERVER_TOKENS_YINGSHI="):
            token = line.split("=",1)[1].strip().strip('"').strip("'")
            break
        elif line.startswith("SUTUI_SERVER_TOKENS_BIHUO="):
            token = line.split("=",1)[1].strip().strip('"').strip("'")
        elif line.startswith("SUTUI_SERVER_TOKENS="):
            token = line.split("=",1)[1].strip().strip('"').strip("'").split(",")[0]

print("Token found:", bool(token), token[:12] if token else "none")

if not token:
    sys.exit(1)

# Fetch tools from local MCP
try:
    r = httpx.post("http://127.0.0.1:8001/", json={"jsonrpc":"2.0","id":"t","method":"tools/list","params":{}}, timeout=5)
    tools = r.json().get("result",{}).get("tools",[])
    print("MCP tools:", len(tools))
except Exception as e:
    print("MCP error:", e)
    tools = []

oai_tools = []
for t in tools:
    oai_tools.append({"type":"function","function":{"name":t["name"],"description":t.get("description","")[:120],"parameters":t.get("inputSchema",{"type":"object","properties":{}})}})

body = {
    "model":"deepseek-chat",
    "messages":[
        {"role":"system","content":"you are lobster assistant with MCP tools"},
        {"role":"user","content":"hi"},
        {"role":"assistant","content":"hello!"},
        {"role":"user","content":"generate a tiger image"},
    ],
    "tools":oai_tools, "tool_choice":"auto", "stream":False
}
body_json = json.dumps(body, ensure_ascii=False)
print("Payload:", len(body_json), "chars, ~%d tokens est" % (len(body_json)//3))

# Test deepseek 3 times
for i in range(3):
    t0 = time.time()
    try:
        c = httpx.Client(timeout=45)
        resp = c.post("https://api.xskill.ai/v1/chat/completions", json=body,
                       headers={"Authorization":"Bearer "+token})
        elapsed = time.time()-t0
        d = resp.json()
        pt = d.get("usage",{}).get("prompt_tokens","?")
        ct = d.get("usage",{}).get("completion_tokens","?")
        fr = d.get("choices",[{}])[0].get("finish_reason","?")
        tc = d.get("choices",[{}])[0].get("message",{}).get("tool_calls")
        content = (d.get("choices",[{}])[0].get("message",{}).get("content") or "")[:100]
        err = d.get("error")
        if err:
            print(" [%d] ERROR: %s elapsed=%.1fs" % (i+1, err, elapsed))
        else:
            has_fake = "tool" in content and ("call" in content or "begin" in content)
            print(" [%d] prompt=%s compl=%s finish=%s real_tools=%s fake_in_text=%s elapsed=%.1fs" % (
                i+1, pt, ct, fr, bool(tc), has_fake, elapsed))
        c.close()
    except Exception as e:
        elapsed = time.time()-t0
        print(" [%d] %s: %s elapsed=%.1fs" % (i+1, type(e).__name__, e, elapsed))

# Test claude
print("\n--- claude-opus-4-6 same payload ---")
body["model"] = "claude-opus-4-6"
for i in range(2):
    t0 = time.time()
    try:
        c = httpx.Client(timeout=45)
        resp = c.post("https://api.xskill.ai/v1/chat/completions", json=body,
                       headers={"Authorization":"Bearer "+token})
        elapsed = time.time()-t0
        d = resp.json()
        pt = d.get("usage",{}).get("prompt_tokens","?")
        fr = d.get("choices",[{}])[0].get("finish_reason","?")
        tc = d.get("choices",[{}])[0].get("message",{}).get("tool_calls")
        err = d.get("error")
        if err:
            print(" [%d] ERROR: %s elapsed=%.1fs" % (i+1, err, elapsed))
        else:
            print(" [%d] prompt=%s finish=%s tools=%s elapsed=%.1fs" % (i+1, pt, fr, bool(tc), elapsed))
        c.close()
    except Exception as e:
        elapsed = time.time()-t0
        print(" [%d] %s: %s elapsed=%.1fs" % (i+1, type(e).__name__, e, elapsed))
