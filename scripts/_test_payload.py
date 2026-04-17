import time, json, httpx, os, sys
sys.path.insert(0, "/opt/lobster-server")
os.chdir("/opt/lobster-server")
from dotenv import load_dotenv
load_dotenv()

token = (os.environ.get("SUTUI_SERVER_TOKENS_YINGSHI") or os.environ.get("SUTUI_SERVER_TOKENS") or os.environ.get("SUTUI_SERVER_TOKEN") or "").split(",")[0].strip()
if not token:
    print("NO TOKEN"); sys.exit(1)

try:
    r = httpx.post("http://127.0.0.1:8001/", json={"jsonrpc":"2.0","id":"t","method":"tools/list","params":{}}, timeout=5)
    tools = r.json().get("result",{}).get("tools",[])
except Exception as e:
    print("MCP error:", e); tools = []

oai_tools = [{"type":"function","function":{"name":t["name"],"description":t.get("description",""),"parameters":t.get("inputSchema",{"type":"object","properties":{}})}} for t in tools]
print("MCP tools:", len(tools), "names:", [t["name"] for t in tools])

body = {
    "model":"deepseek-chat",
    "messages":[
        {"role":"system","content":"you are lobster assistant"},
        {"role":"user","content":"hi"},
        {"role":"assistant","content":"hello!"},
        {"role":"user","content":"list capabilities"},
        {"role":"assistant","content":"let me check..."},
        {"role":"user","content":"generate a tiger image"},
    ],
    "tools":oai_tools, "tool_choice":"auto", "stream":False
}
body_json = json.dumps(body, ensure_ascii=False)
print("Payload:", len(body_json), "chars")

for i in range(3):
    t0 = time.time()
    try:
        c = httpx.Client(timeout=45)
        resp = c.post("https://api.xskill.ai/v1/chat/completions", json=body,
                       headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
        elapsed = time.time()-t0
        d = resp.json()
        pt = d.get("usage",{}).get("prompt_tokens","?")
        ct = d.get("usage",{}).get("completion_tokens","?")
        fr = d.get("choices",[{}])[0].get("finish_reason","?")
        tc = d.get("choices",[{}])[0].get("message",{}).get("tool_calls")
        err = d.get("error")
        if err:
            print(" [%d] ERROR: %s elapsed=%.1fs" % (i+1, err, elapsed))
        else:
            print(" [%d] prompt=%s compl=%s finish=%s tools=%s elapsed=%.1fs" % (i+1, pt, ct, fr, bool(tc), elapsed))
        c.close()
    except Exception as e:
        elapsed = time.time()-t0
        print(" [%d] %s: %s elapsed=%.1fs" % (i+1, type(e).__name__, e, elapsed))

print("\n--- claude-opus-4-6 same payload ---")
body["model"] = "claude-opus-4-6"
t0 = time.time()
try:
    c = httpx.Client(timeout=45)
    resp = c.post("https://api.xskill.ai/v1/chat/completions", json=body,
                   headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
    elapsed = time.time()-t0
    d = resp.json()
    pt = d.get("usage",{}).get("prompt_tokens","?")
    fr = d.get("choices",[{}])[0].get("finish_reason","?")
    tc = d.get("choices",[{}])[0].get("message",{}).get("tool_calls")
    err = d.get("error")
    if err:
        print(" ERROR: %s elapsed=%.1fs" % (err, elapsed))
    else:
        print(" prompt=%s finish=%s tools=%s elapsed=%.1fs" % (pt, fr, bool(tc), elapsed))
    c.close()
except Exception as e:
    elapsed = time.time()-t0
    print(" %s: %s elapsed=%.1fs" % (type(e).__name__, e, elapsed))
