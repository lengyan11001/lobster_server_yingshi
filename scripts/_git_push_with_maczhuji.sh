#!/usr/bin/env bash
# 本地一次性：用 maczhuji + LOBSTER_SSH_KEY_PASSPHRASE 解锁 agent 后 push（勿提交本脚本若含口令）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -f "$ROOT/.env.deploy" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$ROOT/.env.deploy"
  set +a
fi
KEY="${LOBSTER_DEPLOY_SSH_KEY:-/d/maczhuji}"
if [ ! -r "$KEY" ]; then
  echo "[ERR] 私钥不可读: $KEY" >&2
  exit 1
fi
if [ -z "${LOBSTER_SSH_KEY_PASSPHRASE:-}" ]; then
  echo "[ERR] 请在 .env.deploy 配置 LOBSTER_SSH_KEY_PASSPHRASE" >&2
  exit 1
fi
export GIT_TERMINAL_PROMPT=0
AP="$(mktemp)"
{
  echo '#!/usr/bin/env sh'
  echo 'printf %s\\n "$LOBSTER_SSH_KEY_PASSPHRASE"'
} > "$AP"
chmod +x "$AP"
cleanup() { rm -f "$AP"; }
trap cleanup EXIT
eval "$(ssh-agent -s)"
export SSH_ASKPASS_REQUIRE=force
export SSH_ASKPASS="$AP"
export DISPLAY="${DISPLAY:-:0}"
ssh-add "$KEY"
echo "[OK] 已 ssh-add $KEY"
ssh -T git@github.com 2>&1 || true
# git 在 Windows 下若仍走 SSH_ASKPASS 会触发 CreateProcessW 193；密钥已在 agent，无需再弹 askpass
unset SSH_ASKPASS SSH_ASKPASS_REQUIRE DISPLAY
echo "[/run] git push origin main ..."
git push origin main
echo "[OK] git push 完成"
eval "$(ssh-agent -k)" 2>/dev/null || true
