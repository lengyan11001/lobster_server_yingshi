#!/bin/bash
set -e

ASKPASS=$(mktemp)
echo '#!/bin/sh' > "$ASKPASS"
echo 'echo lengyan2' >> "$ASKPASS"
chmod +x "$ASKPASS"

export SSH_ASKPASS="$ASKPASS"
export SSH_ASKPASS_REQUIRE=force
export DISPLAY=:0

eval $(ssh-agent -s) >/dev/null 2>&1
ssh-add /d/maczhuji 2>&1

cd /d/lobster-server

if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
  git add -A
  git commit -m "fix: auto-strip provider prefix (anthropic/ etc) for sutui model ids; restore opus fallback"
fi

echo "=== Push ==="
export GIT_SSH_COMMAND="ssh -p 443 -o StrictHostKeyChecking=no"
git remote set-url origin ssh://git@ssh.github.com:443/lengyan11001/lobster_server.git
git push origin main 2>&1
git remote set-url origin git@github.com:lengyan11001/lobster_server.git

echo "=== Deploy China ==="
ssh -o StrictHostKeyChecking=no root@47.120.39.220 \
  "cd /root/lobster_server && git fetch origin main && git pull origin main && bash scripts/server_update_and_restart.sh" 2>&1

echo "=== Deploy Overseas ==="
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 ubuntu@43.162.93.196 \
  "cd /home/ubuntu/lobster_server && git fetch origin main && git pull origin main && bash scripts/server_update_and_restart.sh" 2>&1 || echo "[WARN] overseas timeout"

kill $SSH_AGENT_PID 2>/dev/null || true
rm -f "$ASKPASS"
echo "=== DONE ==="
