#!/usr/bin/env python3
"""
密码方式 SSH 登录远端执行命令（paramiko）。
当 ssh_run_remote.sh（密钥方式）失败时使用。

用法:
    python scripts/ssh_run_remote_pw.py "ls -la /opt/lobster-server"
    python scripts/ssh_run_remote_pw.py --overseas "cat /opt/lobster-server/.env"

读取 .env.deploy 中的：
    LOBSTER_DEPLOY_HOST / LOBSTER_DEPLOY_HOST_OVERSEAS
    LOBSTER_DEPLOY_PASSWORD / LOBSTER_DEPLOY_PASSWORD_OVERSEAS
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import paramiko


def load_env_deploy() -> dict[str, str]:
    env_file = Path(__file__).resolve().parent.parent / ".env.deploy"
    env = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                env[k.strip()] = v
    return env


def main():
    parser = argparse.ArgumentParser(description="SSH via password (paramiko)")
    parser.add_argument("command", help="Remote command to execute")
    parser.add_argument("--overseas", action="store_true", help="Use overseas server")
    parser.add_argument("--timeout", type=int, default=30, help="Connection timeout")
    args = parser.parse_args()

    env = load_env_deploy()

    if args.overseas:
        host_str = env.get("LOBSTER_DEPLOY_HOST_OVERSEAS", "")
        password = env.get("LOBSTER_DEPLOY_PASSWORD_OVERSEAS", "")
    else:
        host_str = env.get("LOBSTER_DEPLOY_HOST", "")
        password = env.get("LOBSTER_DEPLOY_PASSWORD", "")

    if not host_str:
        print("ERROR: LOBSTER_DEPLOY_HOST not configured in .env.deploy", file=sys.stderr)
        sys.exit(1)
    if not password:
        print("ERROR: LOBSTER_DEPLOY_PASSWORD not configured in .env.deploy", file=sys.stderr)
        sys.exit(1)

    if "@" in host_str:
        user, host = host_str.split("@", 1)
    else:
        user, host = "ubuntu", host_str

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username=user, password=password,
                       timeout=args.timeout, banner_timeout=args.timeout,
                       auth_timeout=args.timeout)
    except Exception as e:
        print(f"ERROR: Cannot connect to {host}: {e}", file=sys.stderr)
        sys.exit(1)

    stdin, stdout, stderr = client.exec_command(args.command, timeout=60)
    out = stdout.read().decode()
    err = stderr.read().decode()
    exit_code = stdout.channel.recv_exit_status()

    if out:
        print(out, end="")
    if err:
        print(err, end="", file=sys.stderr)

    client.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
