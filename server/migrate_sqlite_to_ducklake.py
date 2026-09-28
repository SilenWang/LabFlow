"""现网 sqlite → DuckLake 数据迁移，带行数与内容校验和比对，并 restart 应用层 id 计数器。

约束（沿用 P3 那套约定）：

- 只搬数据不搬结构：目标表由 ``server.db`` 的 DuckLake 建表语句一次成型（与运行时同一条
  ``ATTACH`` / WAL / ``search_path`` 路径），不额外定义 schema。
- 读用 stdlib ``sqlite3``，写走 ``server.db.make_ducklake_engine()``；**不引入 pymysql /
  seekdb**（那条线已作废）。
- 逐表单事务；显式带上 id，原样保留历史主键。
- 目标任一表非空即拒绝执行，避免二次导入写脏。
- 写入前先比对源/目标列集合：源库带模型里已移除的历史列（结构漂移）时以退出码 2 中止，
  目标库不落任何半成品，重跑不会被「目标库非空」挡住。
- 搬完把**应用层 id 计数器**（辅助 SQLite 计数表 ``ids.sqlite``）restart 到 ``max(id)``，
  让下一条新记录续号——DuckLake 没有 sequence（DL0 实测），这是本脚本与 P3 的收尾差异。
- 迁移完成后逐表比对行数与内容校验和。

用法::

    pixi run migrate-ducklake
    pixi run migrate-ducklake -- --sqlite data/labflow.db --ducklake-dir data/ducklake
    pixi run migrate-ducklake -- --dry-run

退出码：0 成功；1 校验不一致；2 预检失败（含源库结构漂移、目标库拒收写入）；
3 目标库非空被拒绝。
"""

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from server.config import DB_PATH
from server.db import (
    DUCKLAKE_ALIAS,
    create_ducklake_tables,
    make_ducklake_engine,
    reset_id_counter,
)
from server.models import Base

# 外键顺序：先父后子。DuckLake 没有 FK 约束（DL0 实测），这个顺序只是让父子关系
# 在数据里先就位，方便人工核对。
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


# 校验和函数与 P3 的 migrate_sqlite_to_seekdb.py 刻意保持一致（同一份算法），但这里
# 复制一份而不是 import：那个模块顶部 import pymysql，本脚本不能把 seekdb 依赖拉进来。
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


def target_column_info(engine, table):
    """目标表的列元信息（``DESCRIBE``：名字、类型、是否可空、默认值）。

    DuckLake 的表挂在 ``dlk`` catalog 下，``DESCRIBE dlk.main.<table>`` 是唯一稳定
    拿到 NOT NULL 信息的入口（duckdb-engine 的 inspector 在 attach 的 catalog 上
    取不到表，``dlk.information_schema`` 也不存在——都实测过）。
    """
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            f"DESCRIBE {DUCKLAKE_ALIAS}.main.{table}"
        ).fetchall()
    return {
        row[0]: {"nullable": row[2] == "YES", "default": row[4]}
        for row in rows
    }


def target_columns(engine, table):
    return list(target_column_info(engine, table))


def column_problems(source_columns, target_info):
    """比对单表的源列与目标列元信息，返回问题描述列表（空列表＝可以搬）。

    - 源库有、目标库没有：``INSERT`` 会撞未知列，因为模型里已移除该历史列。
    - 目标库有、源库没有：源库拿不出这一列的数据，只有该列能留空时才允许
      （例如旧 sqlite 库还没补上 ``batches.remark``）。
    """
    missing = [c for c in source_columns if c not in target_info]
    if missing:
        return [f"目标库缺少源库列 {', '.join(missing)}（模型里已移除的历史列）"]

    source_set = set(source_columns)
    required = [
        c
        for c, meta in target_info.items()
        if c not in source_set and not meta["nullable"] and meta["default"] is None
    ]
    if required:
        return [f"目标库新增必填列 {', '.join(required)}，源库无对应数据"]
    return []


def check_source_columns(engine, source):
    """写入前的结构漂移预检：列不一致就直接中止，目标库一行都不写。"""
    drift = []
    for table in TABLE_ORDER:
        problems = column_problems(
            source[table][0], target_column_info(engine, table)
        )
        drift.extend(f"{table}: {p}" for p in problems)
    if drift:
        raise MigrationError(
            "源库与目标表结构不一致（疑似结构漂移），已中止且未写入: "
            + "; ".join(drift)
        )


def target_count(engine, table):
    with engine.connect() as conn:
        return conn.exec_driver_sql(
            f"SELECT COUNT(*) FROM {DUCKLAKE_ALIAS}.main.{table}"
        ).scalar()


def target_rows(engine, table, columns):
    identifiers = ", ".join(f'"{c}"' for c in columns)
    with engine.connect() as conn:
        return conn.exec_driver_sql(
            f"SELECT {identifiers} FROM {DUCKLAKE_ALIAS}.main.{table} ORDER BY id"
        ).fetchall()


def assert_target_empty(engine):
    filled = []
    for table in TABLE_ORDER:
        count = target_count(engine, table)
        if count:
            filled.append((table, count))
    if filled:
        raise TargetNotEmpty(filled)


def _delete_all_sql(table):
    return text(f"DELETE FROM {DUCKLAKE_ALIAS}.main.{table}")


def discard_partial(engine, tables, ducklake_dir):
    """清空本次已写入的表、并把它们的计数器归零，让目标库回到空库状态。

    只在写入中途失败时调用；此时目标库原本是空的（``assert_target_empty`` 过了），
    所以删掉的都是本次刚写进去的行，重跑不会被退出码 3 挡住。
    """
    if not tables:
        return
    try:
        with engine.begin() as conn:
            for table in reversed(tables):
                conn.execute(_delete_all_sql(table))
        for table in tables:
            reset_id_counter(table, 0, ducklake_dir)
    except Exception as exc:  # pragma: no cover - 目标库已经不健康时才会走到
        print(
            f"[警告] 半成品清理失败，请手动清空目标库: {exc}", file=sys.stderr
        )
        return
    print(
        f"[回滚] 已清空本次写入的表: {', '.join(reversed(tables))}", file=sys.stderr
    )


def insert_table(engine, table, columns, rows):
    """单表单事务写入；显式带 id 以保留历史主键。"""
    if not rows:
        return
    statement = Base.metadata.tables[table].insert()
    payload = [dict(zip(columns, row)) for row in rows]
    with engine.begin() as conn:
        for start in range(0, len(payload), BATCH_SIZE):
            conn.execute(statement, payload[start:start + BATCH_SIZE])


def restart_counter(table, rows, ducklake_dir):
    """把该表的应用层计数器 restart 到源库 max(id)；空表归零（下一次返回 1）。"""
    last_id = max((row[0] for row in rows), default=0)
    reset_id_counter(table, last_id or 0, ducklake_dir)


def verify_table(engine, table, columns, source_rows):
    """比对单表：列集合、行数、内容校验和。"""
    available = target_columns(engine, table)
    missing = [c for c in columns if c not in available]
    if missing:
        raise MigrationError(
            f"目标表 {table} 缺少列: {', '.join(missing)}（先完成模型对齐再迁移）"
        )
    target = target_rows(engine, table, columns)
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


def migrate(sqlite_path, ducklake_dir, dry_run=False):
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
    print(f"目标: {ducklake_dir}", flush=True)

    engine = make_ducklake_engine(ducklake_dir)
    try:
        # 只搬数据不搬结构：目标表由与运行时同一套 DuckLake 建表语句一次成型。
        create_ducklake_tables(engine)
        assert_target_empty(engine)
        # 结构漂移在写第一行之前就拦下：列不一致直接退出码 2，目标库保持空。
        check_source_columns(engine, source)
        written = []
        try:
            for table in TABLE_ORDER:
                columns, rows = source[table]
                insert_table(engine, table, columns, rows)
                written.append(table)
                print(f"  已写入 {table}: {len(rows)} 行")
        except Exception:
            # 写了一半也不能留半成品：清掉本次写入的行，重跑不再被退出码 3 挡住。
            discard_partial(engine, written, ducklake_dir)
            raise
        # 计数器 restart 放在校验之前：即便随后发现内容不一致（退出码 1），
        # 目标库也是自洽的（历史 id 原样保留 + 下一条新记录续号）。
        for table in TABLE_ORDER:
            restart_counter(table, source[table][1], ducklake_dir)
        report = [
            verify_table(engine, table, *source[table]) for table in TABLE_ORDER
        ]
    finally:
        engine.dispose()

    print()
    print_report(report)
    rows_ok = sum(1 for r in report if r["source_rows"] == r["target_rows"])
    sums_ok = sum(1 for r in report if r["source_checksum"] == r["target_checksum"])
    print()
    print(f"行数一致: {rows_ok}/{len(report)}   校验和一致: {sums_ok}/{len(report)}")
    if rows_ok == len(report) and sums_ok == len(report):
        print("迁移完成，校验通过，id 计数器已 restart。")
        return EXIT_OK
    print("迁移完成，但校验不一致，请检查上面的差异。", file=sys.stderr)
    return EXIT_MISMATCH


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="把 sqlite 数据迁移到 DuckLake，比对行数与内容校验和并 restart id 计数器。",
    )
    parser.add_argument("--sqlite", default=str(DB_PATH), help="源 sqlite 库路径")
    parser.add_argument(
        "--ducklake-dir",
        default=str(Path(DB_PATH).parent / "ducklake"),
        help="目标 DuckLake 目录（放 catalog.sqlite / data/ / client.duckdb / ids.sqlite）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只读源库并打印行数/校验和，不写目标"
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        return migrate(args.sqlite, args.ducklake_dir, dry_run=args.dry_run)
    except TargetNotEmpty as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        print("目标库已有数据，未做任何写入。如需重跑请先清空目标库。", file=sys.stderr)
        return EXIT_TARGET_NOT_EMPTY
    except MigrationError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SQLAlchemyError as exc:
        # 兜底：写入阶段的目标库错误（NOT NULL 违反、DuckLake 提交失败等）一律按预检
        # 失败收口，不再带 traceback 以退出码 1（那是「校验不一致」的语义）结束。
        print(f"[失败] 目标库写入失败: {exc}", file=sys.stderr)
        print(
            "常见原因：源库结构漂移（模型里已移除的历史列）、NOT NULL 列为空；"
            "本次写入已回滚，目标库保持空库。",
            file=sys.stderr,
        )
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
