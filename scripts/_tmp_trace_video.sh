#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -f "$ROOT/.env.deploy" ]; then
  set -a
  . "$ROOT/.env.deploy"
  set +a
fi
: "${LOBSTER_DEPLOY_HOST:?missing LOBSTER_DEPLOY_HOST in .env.deploy}"

REMOTE_DIR="${LOBSTER_DEPLOY_REMOTE_DIR:-/opt/lobster-server}"

SSH_BASE="-o StrictHostKeyChecking=accept-new"
_ssh_agent_has_keys() {
  [ -n "${SSH_AUTH_SOCK:-}" ] && ssh-add -l >/dev/null 2>&1
}
_DEPLOY_SSH_AGENT_STARTED=0
_deploy_cleanup_ssh_agent() {
  if [ "$_DEPLOY_SSH_AGENT_STARTED" = 1 ]; then
    eval "$(ssh-agent -k)" 2>/dev/null || true
    _DEPLOY_SSH_AGENT_STARTED=0
  fi
}
if ! _ssh_agent_has_keys; then
  if [ -n "${LOBSTER_DEPLOY_SSH_KEY:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && [ -n "${LOBSTER_SSH_KEY_PASSPHRASE:-}" ]; then
    AP="$(mktemp)"
    { echo '#!/usr/bin/env sh'; echo 'printf %s\\n "$LOBSTER_SSH_KEY_PASSPHRASE"'; } > "$AP"
    chmod +x "$AP"
    trap 'rm -f "$AP"; _deploy_cleanup_ssh_agent' EXIT
    eval "$(ssh-agent -s)"
    _DEPLOY_SSH_AGENT_STARTED=1
    export SSH_ASKPASS_REQUIRE=force SSH_ASKPASS="$AP" DISPLAY="${DISPLAY:-localhost:0}"
    ssh-add "$LOBSTER_DEPLOY_SSH_KEY"
    rm -f "$AP"
    trap _deploy_cleanup_ssh_agent EXIT
  fi
fi
SSH_OPTS_MAIN="$SSH_BASE"
if ! _ssh_agent_has_keys; then
  [ -n "${LOBSTER_DEPLOY_SSH_KEY:-}" ] && [ -r "$LOBSTER_DEPLOY_SSH_KEY" ] && SSH_OPTS_MAIN="-i $LOBSTER_DEPLOY_SSH_KEY $SSH_BASE"
fi

ssh $SSH_OPTS_MAIN "$LOBSTER_DEPLOY_HOST" bash -s <<'REMOTE_EOF'
cd /opt/lobster-server

echo "========== A. Full app.log: trace video.generate tool execution =========="
python3 -c "
import json, sys

with open('logs/app.log', 'r') as f:
    lines = f.readlines()

print(f'Total app.log lines: {len(lines)}')
print()

# Find all lines containing video.generate or run_chat or error
for i, line in enumerate(lines):
    line_stripped = line.strip()
    # Look for invoke_capability video.generate execution (MCP side)
    if 'video.generate' in line_stripped and 'invoke_capability' in line_stripped and '[MCP]' in line_stripped:
        print(f'--- MCP invoke line {i+1} ---')
        print(line_stripped[:1000])
        print()
    # Look for run_chat error
    if 'run_chat error' in line_stripped or 'run_chat_error' in line_stripped:
        print(f'--- run_chat error line {i+1} ---')
        print(line_stripped[:2000])
        print()
    # Look for Traceback or Exception
    if 'Traceback' in line_stripped or 'ModuleNotFoundError' in line_stripped or 'ImportError' in line_stripped:
        print(f'--- Exception line {i+1} ---')
        print(line_stripped[:2000])
        # Print next 10 lines for traceback
        for j in range(1, 11):
            if i+j < len(lines):
                print(lines[i+j].rstrip()[:500])
        print()
    # Look for tool result content containing error
    if '\"content\":' in line_stripped and ('backend.mcp' in line_stripped or 'No module' in line_stripped):
        print(f'--- backend.mcp in content line {i+1} ---')
        print(line_stripped[:3000])
        print()
    # Look for the final reply to user
    if '错误' in line_stripped and ('final_reply' in line_stripped or 'reply_holder' in line_stripped or 'error_holder' in line_stripped):
        print(f'--- final error reply line {i+1} ---')
        print(line_stripped[:2000])
        print()
" 2>&1 || echo "(python error)"

echo ""
echo "========== B. Check chat.py for error handling logic (run_chat) =========="
grep -n 'run_chat\|error_holder\|错误\|_fetch_mcp_tools\|backend\.mcp' backend/app/api/chat.py 2>/dev/null | head -40 || echo "(not found)"

echo ""
echo "========== C. Check sutui_chat_proxy for tool execution loop =========="
grep -n 'invoke_capability\|tool_calls\|tool_result\|_execute_tool\|mcp.*call\|backend\.mcp\|run_chat' backend/app/api/sutui_chat_proxy.py 2>/dev/null | head -40 || echo "(not found)"

echo ""
echo "========== D. Check journalctl for any Traceback since restart =========="
journalctl -u lobster-backend.service --since "2026-04-14 21:24:00" --no-pager 2>/dev/null | grep -iE 'Traceback|Error|Exception|No module|backend\.mcp' | head -20 || echo "(none found)"

echo ""
echo "========== E. Check the FULL conversation that contains backend.mcp error =========="
python3 -c "
import json, sys

with open('logs/app.log', 'r') as f:
    lines = f.readlines()

# Find the conversation where the user gets the backend.mcp error
# Look for tool result messages that contain backend.mcp or No module
for i, line in enumerate(lines):
    if 'backend.mcp' in line or 'No module' in line:
        print(f'=== Line {i+1} contains backend.mcp/No module ===')
        # Extract the relevant part
        if len(line) > 5000:
            # Find the relevant section
            idx = line.find('backend.mcp')
            start = max(0, idx - 500)
            end = min(len(line), idx + 500)
            print(f'...context around match (char {start}-{end})...')
            print(line[start:end])
        else:
            print(line.strip()[:3000])
        print()
" 2>&1 || echo "(python error)"

echo ""
echo "========== DONE =========="
REMOTE_EOF
