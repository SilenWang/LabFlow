#!/usr/bin/env bash
# LabFlow 部署入口：先载入 deploy/labflow.env，再前台运行服务。
# systemd 与手工启动都用它，保证两边读到同一份后端 / 端口配置。
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE="deploy/labflow.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

exec python server.py
