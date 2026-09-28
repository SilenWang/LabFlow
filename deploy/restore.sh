#!/usr/bin/env bash
# 把一份备份还原到全新目录并校验，用来验证备份真的可用。
# 还原只写目标目录，不动线上数据，可以随时演练。
#
# 用法：deploy/restore.sh <备份目录> <新目录>
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP="${1:?用法: deploy/restore.sh <备份目录> <新目录>}"
TARGET="${2:?用法: deploy/restore.sh <备份目录> <新目录>}"
DATABASE="${LABFLOW_DB_NAME:-labflow}"

DIRECT_DUMP=""
if [ -f "$BACKUP" ]; then
  # 兼容老用法：直接给一个 seekdb 的 .sql dump。
  DIRECT_DUMP="$BACKUP"
elif [ ! -d "$BACKUP" ]; then
  echo "找不到备份目录：$BACKUP" >&2
  exit 1
fi

BACKEND=""
if [ -n "$DIRECT_DUMP" ]; then
  BACKEND="seekdb"
elif [ -f "$BACKUP/manifest.txt" ]; then
  BACKEND="$(sed -n 's/^backend=//p' "$BACKUP/manifest.txt" | tail -n 1)"
fi
if [ -z "$BACKEND" ]; then
  # 老备份目录没有 backend= 字段，按目录内容认。
  if [ -d "$BACKUP/ducklake" ]; then BACKEND="ducklake"
  elif [ -f "$BACKUP/$DATABASE.sql" ]; then BACKEND="seekdb"
  elif [ -f "$BACKUP/labflow.db" ]; then BACKEND="sqlite"
  fi
fi
echo "备份后端：${BACKEND:-未知}"

restore_uploads() {
  if [ -f "$BACKUP/uploads.tar.gz" ]; then
    echo "还原上传文件 → $TARGET/uploads"
    mkdir -p "$TARGET"
    tar xzf "$BACKUP/uploads.tar.gz" -C "$TARGET"
  else
    echo "备份里没有上传文件，跳过"
  fi
}

case "$BACKEND" in
  ducklake)
    DEST="$TARGET/data/ducklake"
    if [ -e "$DEST" ]; then
      echo "目标目录已存在，请换一个空的：$DEST" >&2
      exit 1
    fi
    [ -d "$BACKUP/ducklake" ] || { echo "备份里没有 ducklake/ 目录" >&2; exit 1; }

    echo "1/3 还原 DuckLake → $DEST"
    mkdir -p "$TARGET/data"
    cp -a "$BACKUP/ducklake" "$DEST"

    echo "2/3 校验行数"
    if [ -f "$BACKUP/row-counts.json" ]; then
      pixi run python deploy/verify_ducklake.py counts "$DEST" --expect "$BACKUP/row-counts.json"
    else
      pixi run python deploy/verify_ducklake.py counts "$DEST"
    fi

    echo "3/3 还原上传文件"
    restore_uploads
    ;;

  sqlite)
    DEST="$TARGET/data/labflow.db"
    if [ -e "$DEST" ]; then
      echo "目标文件已存在，请换一个空的：$DEST" >&2
      exit 1
    fi
    [ -f "$BACKUP/labflow.db" ] || { echo "备份里没有 labflow.db" >&2; exit 1; }

    echo "1/3 还原 sqlite → $DEST"
    mkdir -p "$TARGET/data"
    cp -- "$BACKUP/labflow.db" "$DEST"

    echo "2/3 校验行数"
    if [ -f "$BACKUP/row-counts.json" ]; then
      pixi run python - "$DEST" "$BACKUP/row-counts.json" <<'PY'
import json, sqlite3, sys
db, expect_file = sys.argv[1], sys.argv[2]
expected = json.loads(open(expect_file, encoding="utf-8").read())
conn = sqlite3.connect(db)
try:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    actual = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
finally:
    conn.close()
problems = [f"{t}: 备份 {expected.get(t)} 行，还原后 {actual.get(t)} 行"
            for t in sorted(set(expected) | set(actual)) if expected.get(t) != actual.get(t)]
for problem in problems:
    print(f"行数不一致：{problem}", file=sys.stderr)
sys.exit(1 if problems else 0)
PY
    else
      echo "备份里没有 row-counts.json，跳过校验"
    fi

    echo "3/3 还原上传文件"
    restore_uploads
    ;;

  seekdb)
    DUMP="${DIRECT_DUMP:-$BACKUP/$DATABASE.sql}"
    [ -f "$DUMP" ] || { echo "找不到 dump 文件：$DUMP" >&2; exit 1; }
    DB_DIR="$TARGET/data/seekdb"
    if [ -e "$DB_DIR" ]; then
      echo "目标目录已存在，请换一个空的：$DB_DIR" >&2
      exit 1
    fi
    mkdir -p "$DB_DIR"

    echo "1/3 还原 dump → $DB_DIR"
    pixi run seekdb-restore "$DB_DIR" "$DUMP"

    echo "2/3 校验行数"
    if [ -f "$BACKUP/row-counts.json" ]; then
      pixi run python deploy/verify_seekdb.py "$DB_DIR" "$DATABASE" --expect "$BACKUP/row-counts.json"
    else
      pixi run python deploy/verify_seekdb.py "$DB_DIR" "$DATABASE"
    fi

    echo "3/3 还原上传文件"
    restore_uploads
    ;;

  *)
    echo "认不出这份备份的后端（没有 manifest.txt，也没有 ducklake/、$DATABASE.sql、labflow.db）" >&2
    exit 1
    ;;
esac

echo "还原完成：$TARGET"
