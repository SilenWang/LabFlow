"""现网 sqlite → seekdb 数据迁移，带行数与内容校验和比对。

约束：
- 只搬数据不搬结构：目标表由 ``Base.metadata.create_all`` 一次成型（与运行时一致）。
- 读用 stdlib ``sqlite3``，写用 ``pymysql``，不引入新依赖。
- 逐表单事务；显式带上 id，保留自增主键。
- 目标任一表非空即拒绝执行，避免二次导入写脏。
- 迁移完成后逐表比对行数与内容校验和。

用法::

    pixi run migrate-seekdb
    pixi run migrate-seekdb -- --sqlite data/labflow.db --seekdb-dir data/seekdb
    pixi run migrate-seekdb -- --dry-run

退出码：0 成功；1 校验不一致；2 预检失败；3 目标库非空被拒绝。
"""

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

import pymysql
from sqlalchemy import create_engine

from server.config import DB_PATH
from server.db import SEEKDB_DATABASE, close_seekdb, open_seekdb
from server.models import Base

# 外键顺序：先父后子，插入期间无需关闭 FK 检查。
TABLE_ORDER = ("users", "projects", "batches", "file_versions")

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2
EXIT_TARGET_NOT_EMPTY = 3

BATCH_SIZE = 500


class MigrationError(Exception):
    """预检或迁移过程中可预期的失败。"""


class TargetNotEmpty(MigrationError):
    """目标库已有数据：拒绝二次导入。"""

    def __init__(self, tables):
        self.tables = list(tables)
        super().__init__(
            "目标库非空，拒绝执行: " + ", ".join(f"{t}({n} 行)" for t, n in self.tables)
        )


def _feed(digest, payload):
    # 长度前缀编码，避免值里出现分隔符时产生歧义。
    digest.update(str(len(payload)).encode("ascii"))
    digest.update(b":")
    digest.update(payload)


def _cell(value):
    """把一个列值归一成字节；NULL 与空串必须区分开。"""
    if value is None:
        return b"\x00NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return b"\x01HEX" + bytes(value).hex().encode("ascii")
    return b"\x02TEXT" + str(value).encode("utf-8")


def rows_checksum(columns, rows):
    """列名 + 逐行内容的 sha256；两侧用同一函数，保证可比。"""
    digest = hashlib.sha256()
    for column in columns:
        _feed(digest, column.encode("utf-8"))
    for row in rows:
        for value in row:
            _feed(digest, _cell(value))
    return digest.hexdigest()


def read_sqlite_table(sqlite_path, table):
    """以只读方式读源库单表，返回 (列名, 按 id 排序的行)。"""
    uri = f"file:{Path(sqlite_path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info('{table}')")]
        if not columns:
            raise MigrationError(f"源库缺少表 {table}: {sqlite_path}")
        identifiers = ", ".join(f'"{c}"' for c in columns)
        rows = conn.execute(
            f'SELECT {identifiers} FROM "{table}" ORDER BY id'
        ).fetchall()
    finally:
        conn.close()
    return columns, rows


def read_source(sqlite_path):
    source = {}
    for table in TABLE_ORDER:
        source[table] = read_sqlite_table(sqlite_path, table)
    return source


def target_columns(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM `{table}`")
        return [row[0] for row in cur.fetchall()]


def target_count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        return cur.fetchone()[0]


def target_rows(conn, table, columns):
    identifiers = ", ".join(f"`{c}`" for c in columns)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {identifiers} FROM `{table}` ORDER BY id")
        return cur.fetchall()


def target_not_null_count(conn, table, column):
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM `{table}` WHERE `{column}` IS NOT NULL")
        return cur.fetchone()[0]


def assert_target_empty(conn):
    filled = []
    for table in TABLE_ORDER:
        count = target_count(conn, table)
        if count:
            filled.append((table, count))
    if filled:
        raise TargetNotEmpty(filled)


def insert_table(conn, table, columns, rows):
    """单表单事务写入；显式带 id 以保留自增主键。"""
    identifiers = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO `{table}` ({identifiers}) VALUES ({placeholders})"
    if rows:
        next_id = max(row[columns.index("id")] for row in rows) + 1
        try:
            with conn.cursor() as cur:
                for start in range(0, len(rows), BATCH_SIZE):
                    cur.executemany(sql, rows[start:start + BATCH_SIZE])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    else:
        next_id = 1
    # ALTER TABLE 会隐式提交，故放在事务之外：把自增值推到 max(id)+1，
    # 避免迁移后新插入的行撞上搬过来的主键。
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = {int(next_id)}")


def verify_table(conn, table, columns, source_rows):
    """比对单表：列集合、行数、内容校验和。"""
    available = target_columns(conn, table)
    missing = [c for c in columns if c not in available]
    if missing:
        raise MigrationError(
            f"目标表 {table} 缺少列: {', '.join(missing)}（先完成模型对齐再迁移）"
        )
    extra = [c for c in available if c not in columns]
    dirty_extra = [c for c in extra if target_not_null_count(conn, table, c)]
    if dirty_extra:
        raise MigrationError(
            f"目标表 {table} 的多余列有数据: {', '.join(dirty_extra)}"
        )
    target = target_rows(conn, table, columns)
    source_sum = rows_checksum(columns, source_rows)
    target_sum = rows_checksum(columns, target)
    ok = len(source_rows) == len(target) and source_sum == target_sum
    return {
        "table": table,
        "source_rows": len(source_rows),
        "target_rows": len(target),
        "source_checksum": source_sum,
        "target_checksum": target_sum,
        "ok": ok,
        "status": "一致" if ok else "不一致",
    }


def print_report(report, dry_run=False):
    width = max(len(r["table"]) for r in report) if report else 8
    print(f"{'表'.ljust(width)}  源行数  目标行数  校验和(源/目标)                 结果")
    for row in report:
        target_sum = row["target_checksum"][:12] if row["target_checksum"] else "-" * 12
        status = row.get("status", "")
        target_rows = "-" if dry_run else row["target_rows"]
        print(
            f"{row['table'].ljust(width)}  "
            f"{row['source_rows']:>6}  {str(target_rows):>8}  "
            f"{row['source_checksum'][:12]} / {target_sum}  {status}"
        )
    if dry_run:
        print("（dry-run：未写目标，以上为源库侧统计）")


def migrate(sqlite_path, seekdb_dir, database=SEEKDB_DATABASE, dry_run=False):
    sqlite_path = Path(sqlite_path)
    if not sqlite_path.is_file():
        raise MigrationError(f"源库不存在: {sqlite_path}")

    source = read_source(sqlite_path)

    if dry_run:
        print(f"[dry-run] 源库: {sqlite_path}")
        report = [
            {
                "table": table,
                "source_rows": len(source[table][1]),
                "target_rows": 0,
                "source_checksum": rows_checksum(*source[table]),
                "target_checksum": "",
            }
            for table in TABLE_ORDER
        ]
        print_report(report, dry_run=True)
        return EXIT_OK

    # flush：目标库非空时拒绝信息走 stderr，先冲掉这两行才不会和报错错位。
    print(f"源库: {sqlite_path}", flush=True)
    print(f"目标: {seekdb_dir} (database={database})", flush=True)

    _, opts = open_seekdb(db_dir=seekdb_dir, database=database)
    try:
        engine = create_engine(
            f"mysql+pymysql://root@localhost/{database}",
            echo=False,
            connect_args={**opts, "charset": "utf8mb4"},
        )
        try:
            # 只搬数据不搬结构：新库由 create_all 一次成型。
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()

        conn = pymysql.connect(
            **opts, database=database, charset="utf8mb4", autocommit=False
        )
        try:
            assert_target_empty(conn)
            for table in TABLE_ORDER:
                columns, rows = source[table]
                insert_table(conn, table, columns, rows)
                print(f"  已写入 {table}: {len(rows)} 行")
            report = [
                verify_table(conn, table, *source[table]) for table in TABLE_ORDER
            ]
        finally:
            conn.close()
    finally:
        close_seekdb()

    print()
    print_report(report)
    rows_ok = sum(1 for r in report if r["source_rows"] == r["target_rows"])
    sums_ok = sum(1 for r in report if r["source_checksum"] == r["target_checksum"])
    print()
    print(f"行数一致: {rows_ok}/{len(report)}   校验和一致: {sums_ok}/{len(report)}")
    if rows_ok == len(report) and sums_ok == len(report):
        print("迁移完成，校验通过。")
        return EXIT_OK
    print("迁移完成，但校验不一致，请检查上面的差异。", file=sys.stderr)
    return EXIT_MISMATCH


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="把 sqlite 数据迁移到 seekdb，并比对行数与内容校验和。",
    )
    parser.add_argument("--sqlite", default=str(DB_PATH), help="源 sqlite 库路径")
    parser.add_argument(
        "--seekdb-dir",
        default=str(Path(DB_PATH).parent / "seekdb"),
        help="目标 seekdb 数据目录",
    )
    parser.add_argument("--database", default=SEEKDB_DATABASE, help="目标库名")
    parser.add_argument(
        "--dry-run", action="store_true", help="只读源库并打印行数/校验和，不写目标"
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        return migrate(
            args.sqlite, args.seekdb_dir, args.database, dry_run=args.dry_run
        )
    except TargetNotEmpty as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        print("目标库已有数据，未做任何写入。如需重跑请先清空目标库。", file=sys.stderr)
        return EXIT_TARGET_NOT_EMPTY
    except MigrationError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return EXIT_USAGE
    except pymysql.err.DataError as exc:
        print(f"[失败] 目标库拒收数据: {exc}", file=sys.stderr)
        print("常见原因：值超出目标列长（models 里的 String 长度需要放宽）。", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())