#!/usr/bin/env bash
# 一键回滚到 sqlite 只读副本。
#
# 做四件事：停服 → 把当前 seekdb 完整备份一份（回滚不丢新数据）→ 用 sqlite
# 快照覆盖 data/labflow.db → 把后端切回 sqlite 并重启服务。
#
# 用法：deploy/rollback-to-sqlite.sh [sqlite 快照路径]
#       不给参数时，自动取 backups/sqlite-frozen-*.db 里最新的一个。
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE="deploy/labflow.env"
SERVICE="${LABFLOW_SERVICE:-labflow}"
AS_ROOT=""
if [ "$(id -u)" -ne 0 ]; then
  AS_ROOT="sudo"
fi

SNAPSHOT="${1:-}"
if [ -z "$SNAPSHOT" ]; then
  SNAPSHOT="$(ls -1t backups/sqlite-frozen-*.db 2>/dev/null | head -n 1 || true)"
fi
if [ -z "$SNAPSHOT" ] || [ ! -f "$SNAPSHOT" ]; then
  echo "找不到 sqlite 快照。请先制作只读回滚点：" >&2
  echo "  mkdir -p backups && cp data/labflow.db backups/sqlite-frozen-\$(date +%F).db" >&2
  exit 1
fi

echo "0/4 使用的 sqlite 快照：$SNAPSHOT"

echo "1/4 停止服务"
if systemctl list-unit-files "$SERVICE.service" >/dev/null 2>&1 \
   && systemctl cat "$SERVICE" >/dev/null 2>&1; then
  $AS_ROOT systemctl stop "$SERVICE"
  SERVICE_INSTALLED=1
else
  echo "  （未安装 systemd 服务，请先手动停掉正在运行的 pixi run serve）"
  SERVICE_INSTALLED=0
fi

echo "2/4 备份当前 seekdb（回滚不丢数据）"
if [ -d data/seekdb ]; then
  bash deploy/backup.sh
else
  echo "  （没有 data/seekdb，跳过）"
fi

echo "3/4 用快照覆盖 data/labflow.db"
mkdir -p data
cp -- "$SNAPSHOT" data/labflow.db

if [ -f "$ENV_FILE" ]; then
  if grep -q '^LABFLOW_DB=' "$ENV_FILE"; then
    sed -i 's/^LABFLOW_DB=.*/LABFLOW_DB=sqlite/' "$ENV_FILE"
  else
    echo 'LABFLOW_DB=sqlite' >> "$ENV_FILE"
  fi
else
  echo "LABFLOW_DB=sqlite" > "$ENV_FILE"
fi
echo "  后端已切回 sqlite（$ENV_FILE）"

echo "4/4 启动服务"
if [ "$SERVICE_INSTALLED" = "1" ]; then
  $AS_ROOT systemctl start "$SERVICE"
  $AS_ROOT systemctl --no-pager --lines=0 status "$SERVICE" || true
else
  echo "  请手动执行：pixi run serve"
fi

echo "回滚完成。sqlite 已恢复为线上库；切换前的 seekdb 数据在上一步的备份目录里。"
