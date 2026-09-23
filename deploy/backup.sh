#!/usr/bin/env bash
# LabFlow 备份：用 seekdb-dump 导出数据库，打包上传文件，并生成校验清单。
# 服务运行中也能执行（seekdb-dump 连的是运行实例，不必停服）。
#
# 用法：deploy/backup.sh [备份根目录]     默认 backups/
set -euo pipefail

cd "$(dirname "$0")/.."

DEST="${1:-backups}"
DB_DIR="${LABFLOW_SEEKDB_DIR:-data/seekdb}"
DATABASE="${LABFLOW_DB_NAME:-labflow}"
KEEP="${LABFLOW_KEEP_BACKUPS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/labflow-$STAMP"

if [ ! -d "$DB_DIR" ]; then
  echo "找不到 seekdb 数据目录：$DB_DIR（服务是否已用 LABFLOW_DB=seekdb 启动过？）" >&2
  exit 1
fi

mkdir -p "$OUT"

echo "1/4 导出 seekdb → $OUT/$DATABASE.sql"
pixi run seekdb-dump "$DB_DIR" --database "$DATABASE" -o "$OUT/$DATABASE.sql"

echo "2/4 记录行数（供还原时校验）"
pixi run python deploy/verify_seekdb.py "$DB_DIR" "$DATABASE" > "$OUT/row-counts.json"
cat "$OUT/row-counts.json"

echo "3/4 打包上传文件 → $OUT/uploads.tar.gz"
tar czf "$OUT/uploads.tar.gz" uploads

echo "4/4 生成校验清单"
SEEKDB_VERSION="$(pixi run python -c 'import importlib.metadata as m; print(m.version("pylibseekdb"))')"
{
  echo "created_at=$STAMP"
  echo "seekdb_dir=$DB_DIR"
  echo "database=$DATABASE"
  echo "seekdb_version=$SEEKDB_VERSION"
} > "$OUT/manifest.txt"

# 有 sqlite 只读回滚点时一并收进备份，方便一键回滚
if [ -f data/labflow.db ]; then
  cp data/labflow.db "$OUT/labflow.db"
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
