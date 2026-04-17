"""Diagnose xskill.ai proxy latency vs direct API from the server."""
import paramiko, json

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)

def run(cmd, timeout=180):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return out, err

print("=== 1. TCP connectivity to xskill.ai ===")
out, _ = run("curl -w 'dns:%{time_namelookup} connect:%{time_connect} tls:%{time_appconnect} total:%{time_total}\\n' -so /dev/null https://api.xskill.ai/v1/models -H 'Authorization: Bearer test' --max-time 10")
print(out)

print("\n=== 2. DNS resolution ===")
out, _ = run("dig +short api.xskill.ai 2>/dev/null || nslookup api.xskill.ai 2>/dev/null | tail -5")
print(out)

print("\n=== 3. Traceroute to xskill.ai (first 10 hops) ===")
out, _ = run("traceroute -n -m 10 -w 2 api.xskill.ai 2>/dev/null | head -12 || echo 'traceroute not available'", timeout=30)
print(out)

print("\n=== 4. Test xskill.ai with SMALL request (no tools, simple question) ===")
# Read the actual token from the server .env
out_token, _ = run("cd /opt/lobster-server && python3 -c \"from dotenv import load_dotenv; load_dotenv(); import os; print(os.environ.get('SUTUI_SERVER_TOKENS','')[:80])\"")
token = out_token.strip().split(",")[0].strip() if out_token.strip() else ""
print(f"  Token available: {'yes' if token else 'NO'}")

if token:
    # Small request - no tools
    small_body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False
    })
    print("\n  --- deepseek-chat via xskill (no tools, 'hi') ---")
    out, err = run(f"""curl -s -w '\\n__TIMING__ dns:%{{time_namelookup}} connect:%{{time_connect}} tls:%{{time_appconnect}} firstbyte:%{{time_starttransfer}} total:%{{time_total}}' \\
        -X POST https://api.xskill.ai/v1/chat/completions \\
        -H 'Authorization: Bearer {token}' \\
        -H 'Content-Type: application/json' \\
        --max-time 30 \\
        -d '{small_body}'""", timeout=40)
    lines = out.split("\n")
    for l in lines:
        if "__TIMING__" in l:
            print(f"  TIMING: {l.replace('__TIMING__ ','')}")
        elif "prompt_tokens" in l or "error" in l:
            try:
                d = json.loads(l)
                usage = d.get("usage", {})
                print(f"  prompt_tokens={usage.get('prompt_tokens')} completion_tokens={usage.get('completion_tokens')} model={d.get('model')}")
                billing = d.get("x_billing", {})
                if billing:
                    print(f"  x_billing: credits_used={billing.get('credits_used')} balance={billing.get('balance')}")
            except:
                print(f"  {l[:300]}")

    # Request WITH tools (mimics real usage)
    tools_body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": "you are a helper"}, {"role": "user", "content": "generate a tiger image"}],
        "tools": [{"type":"function","function":{"name":"generate","description":"gen image","parameters":{"type":"object","properties":{"prompt":{"type":"string"}},"required":["prompt"]}}}],
        "tool_choice": "auto",
        "stream": False
    })
    print("\n  --- deepseek-chat via xskill (WITH 1 tool, 'generate tiger') ---")
    out, err = run(f"""curl -s -w '\\n__TIMING__ dns:%{{time_namelookup}} connect:%{{time_connect}} tls:%{{time_appconnect}} firstbyte:%{{time_starttransfer}} total:%{{time_total}}' \\
        -X POST https://api.xskill.ai/v1/chat/completions \\
        -H 'Authorization: Bearer {token}' \\
        -H 'Content-Type: application/json' \\
        --max-time 60 \\
        -d '{tools_body}'""", timeout=70)
    lines = out.split("\n")
    for l in lines:
        if "__TIMING__" in l:
            print(f"  TIMING: {l.replace('__TIMING__ ','')}")
        elif "prompt_tokens" in l or "error" in l or "tool_calls" in l:
            try:
                d = json.loads(l)
                usage = d.get("usage", {})
                choices = d.get("choices", [{}])
                fr = choices[0].get("finish_reason", "?") if choices else "?"
                tc = choices[0].get("message", {}).get("tool_calls") if choices else None
                print(f"  prompt_tokens={usage.get('prompt_tokens')} finish_reason={fr} has_tool_calls={bool(tc)} model={d.get('model')}")
            except:
                print(f"  {l[:300]}")

    # Test claude via xskill
    claude_body = json.dumps({
        "model": "claude-opus-4-6",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False
    })
    print("\n  --- claude-opus-4-6 via xskill (no tools, 'hi') ---")
    out, err = run(f"""curl -s -w '\\n__TIMING__ dns:%{{time_namelookup}} connect:%{{time_connect}} tls:%{{time_appconnect}} firstbyte:%{{time_starttransfer}} total:%{{time_total}}' \\
        -X POST https://api.xskill.ai/v1/chat/completions \\
        -H 'Authorization: Bearer {token}' \\
        -H 'Content-Type: application/json' \\
        --max-time 30 \\
        -d '{claude_body}'""", timeout=40)
    lines = out.split("\n")
    for l in lines:
        if "__TIMING__" in l:
            print(f"  TIMING: {l.replace('__TIMING__ ','')}")
        elif "prompt_tokens" in l or "error" in l:
            try:
                d = json.loads(l)
                usage = d.get("usage", {})
                err_msg = d.get("error", {})
                if err_msg:
                    print(f"  ERROR: {err_msg}")
                else:
                    print(f"  prompt_tokens={usage.get('prompt_tokens')} completion_tokens={usage.get('completion_tokens')} model={d.get('model')}")
            except:
                print(f"  {l[:300]}")

print("\n=== 5. Check xskill.ai recent error pattern from server logs ===")
out, _ = run("grep 'xskill' /opt/lobster-server/logs/app.log | grep -iE 'timeout|error|fail|502|503|504' | tail -10")
print(out or "(none)")

print("\n=== 6. Check if connection pool is stuck ===")
out, _ = run("ss -tnp | grep -i xskill 2>/dev/null || netstat -tnp 2>/dev/null | grep xskill | head -10")
print(out or "(no active connections)")

ssh.close()
