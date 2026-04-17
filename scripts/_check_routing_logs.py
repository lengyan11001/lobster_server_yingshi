import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("42.194.209.150", username="ubuntu", password="|EP^q4r5-)f2k", timeout=15)
_, stdout, _ = ssh.exec_command("tail -300 /opt/lobster-server/logs/app.log | grep -E 'attempts=|route|direct|api.deepseek' | tail -20", timeout=10)
out = stdout.read().decode("utf-8", errors="replace").strip()
print("ROUTING LOGS:")
print(out if out else "(no matching logs - waiting for user requests)")

print("\nRECENT CHAT TRACES:")
_, stdout, _ = ssh.exec_command("tail -300 /opt/lobster-server/logs/app.log | grep 'chat_trace' | tail -8", timeout=10)
print(stdout.read().decode("utf-8", errors="replace").strip()[:3000])

print("\nSERVICE UP:")
_, stdout, _ = ssh.exec_command("ps aux | grep 'backend.run' | grep -v grep | wc -l", timeout=5)
print(stdout.read().decode().strip(), "processes")

ssh.close()
