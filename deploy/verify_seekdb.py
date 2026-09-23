"""统计嵌入式 seekdb 实例的表行数，供备份 / 还原校验使用。

用法：
    verify_seekdb.py DB_DIR [DATABASE] [--expect FILE]

默认输出一份 JSON（表名 -> 行数）到 stdout。带上 --expect FILE 时，会把当前
行数与 FILE 里记录的 JSON 逐表比对，不一致则以退出码 1 结束并打印差异。
"""

from __future__ import annotations

import argparse
import json
import sys

import pymysql
import pylibseekdb


def collect_counts(db_dir: str, database: str) -> dict[str, int]:
    instance = pylibseekdb.open(db_dir)
    try:
        conn = pymysql.connect(
            charset="utf8mb4",
            autocommit=True,
            **dict(instance.connection_options()),
        )
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY table_name",
                (database,),
            )
            tables = [row[0] for row in cur.fetchall()]
            counts = {}
            for table in tables:
                cur.execute(f"SELECT COUNT(*) FROM `{database}`.`{table}`")
                counts[table] = int(cur.fetchone()[0])
        finally:
            conn.close()
    finally:
        instance.close()
        pylibseekdb.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_dir")
    parser.add_argument("database", nargs="?", default="labflow")
    parser.add_argument("--expect", metavar="FILE")
    args = parser.parse_args(argv)

    actual = collect_counts(args.db_dir, args.database)
    print(json.dumps(actual, ensure_ascii=False, sort_keys=True))

    if not args.expect:
        return 0

    with open(args.expect, encoding="utf-8") as fh:
        expected = json.load(fh)
    if expected == actual:
        print(f"行数校验通过：{actual}", file=sys.stderr)
        return 0

    print("行数校验失败：", file=sys.stderr)
    for table in sorted(set(expected) | set(actual)):
        want, got = expected.get(table), actual.get(table)
        if want != got:
            print(f"  {table}: 期望 {want}，实际 {got}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
