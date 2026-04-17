"""Test direct deepseek API vs xskill.ai from the server."""
import paramiko, json

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)

def run(cmd, timeout=60):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    return stdout.read().decode("utf-8", errors="replace").strip()

# Read xskill token
xskill_token = ""
for line in run("cat /opt/lobster-server/.env").split("\n"):
    if line.startswith("SUTUI_SERVER_TOKENS_YINGSHI="):
        xskill_token = line.split("=",1)[1].strip().strip('"')
        break

ds_token = "sk-1f6b07ab8b4a4dccb9444e774d124e67"

print("=== Direct DeepSeek API (api.deepseek.com) ===")
print("--- No tools, simple 'hi' ---")
for i in range(3):
    body = json.dumps({"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}],"stream":False})
    out = run(
        "curl -s -w '\\nTIME:%%{time_total}s TTFB:%%{time_starttransfer}s' "
        "-X POST https://api.deepseek.com/v1/chat/completions "
        "-H 'Authorization: Bearer %s' "
        "-H 'Content-Type: application/json' "
        "--max-time 30 "
        "-d '%s'" % (ds_token, body), timeout=40)
    lines = out.split("\n")
    timing = [l for l in lines if "TIME:" in l]
    body_lines = [l for l in lines if "TIME:" not in l]
    try:
        d = json.loads("\n".join(body_lines))
        pt = d.get("usage",{}).get("prompt_tokens","?")
        ct = d.get("usage",{}).get("completion_tokens","?")
        err = d.get("error")
        if err:
            print("  [%d] ERROR: %s %s" % (i+1, err, timing[0] if timing else ""))
        else:
            print("  [%d] prompt=%s compl=%s %s" % (i+1, pt, ct, timing[0] if timing else ""))
    except:
        print("  [%d] %s %s" % (i+1, "\n".join(body_lines)[:200], timing[0] if timing else ""))

print("\n--- With tools, 'generate tiger' ---")
tools_body = json.dumps({
    "model":"deepseek-chat",
    "messages":[
        {"role":"system","content":"you are a helper with tools"},
        {"role":"user","content":"generate a tiger image"}
    ],
    "tools":[
        {"type":"function","function":{"name":"image_generate","description":"generate image from prompt","parameters":{"type":"object","properties":{"prompt":{"type":"string"},"model":{"type":"string"}},"required":["prompt"]}}},
        {"type":"function","function":{"name":"list_capabilities","description":"list available capabilities","parameters":{"type":"object","properties":{}}}},
        {"type":"function","function":{"name":"video_generate","description":"generate video","parameters":{"type":"object","properties":{"prompt":{"type":"string"},"duration":{"type":"integer"}},"required":["prompt"]}}}
    ],
    "tool_choice":"auto","stream":False
})
for i in range(3):
    out = run(
        "curl -s -w '\\nTIME:%%{time_total}s TTFB:%%{time_starttransfer}s' "
        "-X POST https://api.deepseek.com/v1/chat/completions "
        "-H 'Authorization: Bearer %s' "
        "-H 'Content-Type: application/json' "
        "--max-time 30 "
        "-d '%s'" % (ds_token, tools_body.replace("'","'\"'\"'")), timeout=40)
    lines = out.split("\n")
    timing = [l for l in lines if "TIME:" in l]
    body_lines = [l for l in lines if "TIME:" not in l]
    try:
        d = json.loads("\n".join(body_lines))
        pt = d.get("usage",{}).get("prompt_tokens","?")
        fr = d.get("choices",[{}])[0].get("finish_reason","?")
        tc = d.get("choices",[{}])[0].get("message",{}).get("tool_calls")
        content = (d.get("choices",[{}])[0].get("message",{}).get("content") or "")[:80]
        err = d.get("error")
        if err:
            print("  [%d] ERROR: %s %s" % (i+1, err, timing[0] if timing else ""))
        else:
            print("  [%d] prompt=%s finish=%s tool_calls=%s %s" % (i+1, pt, fr, bool(tc), timing[0] if timing else ""))
            if content and not tc:
                print("       content: %s" % content)
    except:
        print("  [%d] %s %s" % (i+1, "\n".join(body_lines)[:200], timing[0] if timing else ""))

print("\n\n=== xskill.ai (same requests for comparison) ===")
if xskill_token:
    print("--- No tools, 'hi' ---")
    for i in range(3):
        body = json.dumps({"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}],"stream":False})
        out = run(
            "curl -s -w '\\nTIME:%%{time_total}s TTFB:%%{time_starttransfer}s' "
            "-X POST https://api.xskill.ai/v1/chat/completions "
            "-H 'Authorization: Bearer %s' "
            "-H 'Content-Type: application/json' "
            "--max-time 30 "
            "-d '%s'" % (xskill_token, body), timeout=40)
        lines = out.split("\n")
        timing = [l for l in lines if "TIME:" in l]
        body_lines = [l for l in lines if "TIME:" not in l]
        try:
            d = json.loads("\n".join(body_lines))
            pt = d.get("usage",{}).get("prompt_tokens","?")
            ct = d.get("usage",{}).get("completion_tokens","?")
            err = d.get("error")
            if err:
                print("  [%d] ERROR: %s %s" % (i+1, err, timing[0] if timing else ""))
            else:
                print("  [%d] prompt=%s compl=%s %s" % (i+1, pt, ct, timing[0] if timing else ""))
        except:
            print("  [%d] %s %s" % (i+1, "\n".join(body_lines)[:200], timing[0] if timing else ""))

    print("\n--- With tools, 'generate tiger' ---")
    for i in range(3):
        out = run(
            "curl -s -w '\\nTIME:%%{time_total}s TTFB:%%{time_starttransfer}s' "
            "-X POST https://api.xskill.ai/v1/chat/completions "
            "-H 'Authorization: Bearer %s' "
            "-H 'Content-Type: application/json' "
            "--max-time 30 "
            "-d '%s'" % (xskill_token, tools_body.replace("'","'\"'\"'")), timeout=40)
        lines = out.split("\n")
        timing = [l for l in lines if "TIME:" in l]
        body_lines = [l for l in lines if "TIME:" not in l]
        try:
            d = json.loads("\n".join(body_lines))
            pt = d.get("usage",{}).get("prompt_tokens","?")
            fr = d.get("choices",[{}])[0].get("finish_reason","?")
            tc = d.get("choices",[{}])[0].get("message",{}).get("tool_calls")
            err = d.get("error")
            if err:
                print("  [%d] ERROR: %s %s" % (i+1, err, timing[0] if timing else ""))
            else:
                print("  [%d] prompt=%s finish=%s tool_calls=%s %s" % (i+1, pt, fr, bool(tc), timing[0] if timing else ""))
        except:
            print("  [%d] %s %s" % (i+1, "\n".join(body_lines)[:200], timing[0] if timing else ""))
else:
    print("  No xskill token found, skipping")

print("\nDone")
