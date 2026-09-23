#!/usr/bin/env bash
# 把一份备份还原到全新目录并校验，用来验证备份真的可用。
# 还原只写目标目录，不动线上数据，可以随时演练。
#
# 用法：deploy/restore.sh <备份目录或 .sql 文件> <新目录>
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP="${1:?用法: deploy/restore.sh <备份目录或 .sql 文件> <新目录>}"
TARGET="${2:?用法: deploy/restore.sh <备份目录或 .sql 文件> <新目录>}"
DATABASE="${LABFLOW_DB_NAME:-labflow}"

if [ -d "$BACKUP" ]; then
  DUMP="$BACKUP/$DATABASE.sql"
  EXPECT="$BACKUP/row-counts.json"
else
  DUMP="$BACKUP"
  EXPECT=""
fi
[ -f "$DUMP" ] || { echo "找不到 dump 文件：$DUMP" >&2; exit 1; }

# 目录布局与仓库一致：数据库落在 <目标>/data/seekdb，上传文件落在 <目标>/uploads
DB_DIR="$TARGET/data/seekdb"
if [ -e "$DB_DIR" ]; then
  echo "目标目录已存在，请换一个空的：$DB_DIR" >&2
  exit 1
fi
mkdir -p "$DB_DIR"

echo "1/3 还原 dump → $DB_DIR"
pixi run seekdb-restore "$DB_DIR" "$DUMP"

echo "2/3 校验行数"
if [ -n "$EXPECT" ] && [ -f "$EXPECT" ]; then
  pixi run python deploy/verify_seekdb.py "$DB_DIR" "$DATABASE" --expect "$EXPECT"
else
  pixi run python deploy/verify_seekdb.py "$DB_DIR" "$DATABASE"
fi

if [ -f "$BACKUP/uploads.tar.gz" ]; then
  echo "3/3 还原上传文件 → $TARGET/uploads"
  mkdir -p "$TARGET"
  tar xzf "$BACKUP/uploads.tar.gz" -C "$TARGET"
else
  echo "3/3 备份里没有上传文件，跳过"
fi

echo "还原完成：$TARGET"
