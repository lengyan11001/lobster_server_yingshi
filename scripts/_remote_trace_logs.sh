#!/usr/bin/env bash
# Usage:
#   ./scripts/_remote_trace_logs.sh <trace_id_1> [trace_id_2 ...]
#
# Prints matching lines from remote logs for the given trace ids.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <trace_id_1> [trace_id_2 ...]" >&2
  exit 2
fi

# shellcheck disable=SC2034
TIDS=("$@")

REMOTE_DIR="${LOBSTER_DEPLOY_REMOTE_DIR:-/opt/lobster-server}"

./scripts/ssh_run_remote.sh bash -s <<'EOF'
set -euo pipefail

cd "${LOBSTER_DEPLOY_REMOTE_DIR:-/opt/lobster-server}" || cd /opt/lobster-server

python3 - <<'PY'
import os
import pathlib

tids = os.environ.get("TRACE_IDS", "").split(",")
tids = [t.strip() for t in tids if t.strip()]

paths = [
    "backend.log",
    "logs/app.log",
]

for p in paths:
    fp = pathlib.Path(p)
    print("====", p, "====")
    if not fp.exists():
        print("NOFILE")
        continue
    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()[-12000:]
    out = []
    for ln in lines:
        if any(t in ln for t in tids):
            out.append(ln)
            continue
        if "sutui_chat_completions" in ln or "chat_trace" in ln:
            out.append(ln)
    for ln in out[-200:]:
        print(ln)
    print("MATCH", len(out))
PY
EOF

