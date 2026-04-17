#!/usr/bin/env bash
# 非交互：从 .env.deploy 读 LOBSTER_SSH_KEY_PASSPHRASE，用 SSH_ASKPASS 解锁密钥后 git push。
# 默认解锁 LOBSTER_DEPLOY_SSH_KEY；未设置时尝试 D:/maczhuji（与常见本机 Git 配置一致）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ -f "$ROOT/.env.deploy" ]; then
  set -a
  # shellcheck source=../.env.deploy
  . "$ROOT/.env.deploy"
  set +a
fi
export GIT_TERMINAL_PROMPT=0
KEY="${LOBSTER_DEPLOY_SSH_KEY:-D:/maczhuji}"
if [ ! -r "$KEY" ]; then
  echo "[ERR] 私钥不可读: $KEY" >&2
  exit 1
fi
if ! ssh-keygen -y -f "$KEY" -P "" >/dev/null 2>&1; then
  if [ -z "${LOBSTER_SSH_KEY_PASSPHRASE:-}" ]; then
    echo "[ERR] 私钥已加密，请在 .env.deploy 配置 LOBSTER_SSH_KEY_PASSPHRASE" >&2
    exit 1
  fi
fi
_eval="$(ssh-agent -s)"
eval "$_eval"
_cleanup() {
  ssh-agent -k >/dev/null 2>&1 || true
}
trap _cleanup EXIT
AP="$(mktemp)"
{
  echo '#!/usr/bin/env sh'
  echo 'printf %s\\n "$LOBSTER_SSH_KEY_PASSPHRASE"'
} > "$AP"
chmod +x "$AP"
export SSH_ASKPASS_REQUIRE=force
export SSH_ASKPASS="$AP"
export DISPLAY="${DISPLAY:-localhost:0}"
ssh-add "$KEY"
rm -f "$AP"
# 解锁后清掉 ASKPASS，避免后续 ssh 再弹
unset SSH_ASKPASS SSH_ASKPASS_REQUIRE
# 关键：用 Git Bash 自带的 ssh（共享当前 ssh-agent），不用 Windows OpenSSH（agent 不互通）
# Git Bash 自带的 ssh.exe（与当前 ssh-agent 共享）；用 8.3 短路径避免空格
export GIT_SSH_COMMAND="C:/PROGRA~1/Git/usr/bin/ssh.exe"
export GIT_SSH_VARIANT=ssh
echo "[push] 使用 SSH: $GIT_SSH_COMMAND，agent keys:"
ssh-add -l
git push origin main
unset GIT_SSH_COMMAND
trap - EXIT
ssh-agent -k >/dev/null 2>&1 || true
bash "$ROOT/scripts/deploy_from_local.sh"
