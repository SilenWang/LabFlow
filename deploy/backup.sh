#!/usr/bin/env bash
# LabFlow 备份：按后端分两条路，产物都是一份目录 + 校验清单，能在干净目录还原。
#
#   - ducklake：DuckDB/DuckLake 带 WAL，**不能拷活文件** → 停服拷贝整个 data/ducklake。
#   - sqlite  ：VACUUM INTO 做一致性快照（在线安全，不是拷活文件）。
#
# 用法：deploy/backup.sh [备份根目录]     默认 backups/
set -euo pipefail

cd "$(dirname "$0")/.."

DEST="${1:-backups}"
KEEP="${LABFLOW_KEEP_BACKUPS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/labflow-$STAMP"
SERVICE="${LABFLOW_SERVICE:-labflow}"
AS_ROOT=""
if [ "$(id -u)" -ne 0 ]; then
  AS_ROOT="sudo"
fi
SERVICE_STOPPED=0

# 后端：命令行环境优先，其次 deploy/labflow.env，最后与 config.py 一样默认 sqlite。
if [ -z "${LABFLOW_DB:-}" ] && [ -f deploy/labflow.env ]; then
  LABFLOW_DB="$(sed -n 's/^LABFLOW_DB=//p' deploy/labflow.env | tail -n 1)"
fi
BACKEND="${LABFLOW_DB:-sqlite}"

stop_service() {
  if systemctl cat "$SERVICE" >/dev/null 2>&1; then
    $AS_ROOT systemctl stop "$SERVICE"
    SERVICE_STOPPED=1
  fi
}

start_service() {
  if [ "$SERVICE_STOPPED" = "1" ]; then
    SERVICE_STOPPED=0
    $AS_ROOT systemctl start "$SERVICE"
  fi
}

# 出错也把服务起回来，别让一次失败的备份把线上停着。
restart_on_exit() {
  if [ "$SERVICE_STOPPED" = "1" ]; then
    SERVICE_STOPPED=0
    $AS_ROOT systemctl start "$SERVICE" || true
  fi
}
trap restart_on_exit EXIT

mkdir -p "$OUT"

echo "后端：$BACKEND"
case "$BACKEND" in
  ducklake)
    DUCKLAKE_DIR="${LABFLOW_DUCKLAKE_DIR:-data/ducklake}"
    if [ ! -d "$DUCKLAKE_DIR" ]; then
      echo "找不到 DuckLake 目录：$DUCKLAKE_DIR（服务是否已用 LABFLOW_DB=ducklake 启动过？）" >&2
      exit 1
    fi

    echo "1/4 停服，并确认没有进程占着 DuckLake 文件"
    stop_service
    if ! pixi run python deploy/verify_ducklake.py probe-unlocked "$DUCKLAKE_DIR"; then
      echo "DuckDB 是进程独占锁，库还被占着就不能拷（WAL 下活拷可能拿到半个状态）。" >&2
      echo "请先停掉正在运行的 LabFlow（systemctl stop labflow 或 Ctrl+C 掉 pixi run serve）再备份。" >&2
      exit 1
    fi

    echo "2/4 拷贝 $DUCKLAKE_DIR → $OUT/ducklake"
    cp -a "$DUCKLAKE_DIR" "$OUT/ducklake"

    echo "3/4 记录行数（从副本里读，供还原时校验）"
    pixi run python deploy/verify_ducklake.py counts "$OUT/ducklake" > "$OUT/row-counts.json"
    cat "$OUT/row-counts.json"

    echo "4/4 写清单"
    {
      echo "created_at=$STAMP"
      echo "backend=ducklake"
      echo "ducklake_dir=$DUCKLAKE_DIR"
      echo "catalog=$DUCKLAKE_DIR/catalog.sqlite"
    } > "$OUT/manifest.txt"

    start_service
    ;;

  sqlite)
    if [ ! -f data/labflow.db ]; then
      echo "找不到 sqlite 库：data/labflow.db" >&2
      exit 1
    fi

    echo "1/3 VACUUM INTO 一致性快照 → $OUT/labflow.db"
    pixi run python - "$OUT/labflow.db" <<'PY'
import sqlite3, sys
conn = sqlite3.connect("data/labflow.db", timeout=30)
try:
    conn.execute("VACUUM INTO ?", (sys.argv[1],))
finally:
    conn.close()
PY

    echo "2/3 记录行数"
    pixi run python - "$OUT/labflow.db" > "$OUT/row-counts.json" <<'PY'
import json, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
try:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
finally:
    conn.close()
print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
PY
    cat "$OUT/row-counts.json"

    echo "3/3 写清单"
    {
      echo "created_at=$STAMP"
      echo "backend=sqlite"
      echo "database=labflow.db"
    } > "$OUT/manifest.txt"
    ;;

  *)
    echo "不认识的 LABFLOW_DB=$BACKEND（可选：ducklake / sqlite）" >&2
    exit 1
    ;;
esac

if [ -d uploads ]; then
  echo "打包上传文件 → $OUT/uploads.tar.gz"
  tar czf "$OUT/uploads.tar.gz" uploads
else
  echo "没有 uploads 目录，跳过上传文件打包"
fi

( cd "$OUT" && find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%P\n' | sort | xargs sha256sum > SHA256SUMS )

# 轮转：只清理本脚本生成的、且超出保留份数的旧备份
if [ "$KEEP" -gt 0 ]; then
  mapfile -t stale < <(ls -1dt "$DEST"/labflow-* 2>/dev/null | tail -n +"$((KEEP + 1))")
  for old in "${stale[@]}"; do
    case "$(basename "$old")" in
      labflow-*) rm -rf -- "$old"; echo "已清理旧备份：$old" ;;
    esac
  done
fi

echo "备份完成：$OUT"
