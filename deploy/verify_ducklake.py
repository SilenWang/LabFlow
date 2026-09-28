"""DuckLake 备份辅助：数行数、探锁，供 backup.sh / restore.sh 使用。

用法：
    verify_ducklake.py counts DUCKLAKE_DIR [--expect FILE]
    verify_ducklake.py probe-unlocked DUCKLAKE_DIR

DUCKLAKE_DIR 是 `data/ducklake` 这种目录（里面是 catalog.sqlite + data/ + client.duckdb）。

counts：只读 attach 一份 DuckLake 目录，按表打印 JSON（表名 -> 行数）；带 --expect
时与备份里的 row-counts.json 逐表比对，不一致则退出码 1。

probe-unlocked：DuckDB 是进程独占锁，只要还有进程开着这个库，就别拷文件（有 WAL，
活拷可能拿到半个状态）。这里试着以读写方式打开 client.duckdb：打不开说明库还被占着。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb


def _attach(conn, ducklake_dir: Path, alias: str, read_only: bool) -> None:
    catalog = ducklake_dir / "catalog.sqlite"
    data_dir = ducklake_dir / "data"
    conn.execute("LOAD ducklake; LOAD sqlite")
    mode = ", READ_ONLY" if read_only else ""
    # 副本会落在别的目录，而 catalog 里记的是建库时的绝对数据路径——不覆盖就挂不上。
    conn.execute(
        f"ATTACH 'ducklake:sqlite:{catalog}' AS {alias} "
        f"(DATA_PATH '{data_dir}', OVERRIDE_DATA_PATH true{mode})"
    )


def collect_counts(ducklake_dir: Path) -> dict[str, int]:
    conn = duckdb.connect(":memory:")
    try:
        _attach(conn, ducklake_dir, "dl", read_only=True)
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_catalog = 'dl' ORDER BY table_name"
            ).fetchall()
        ]
        return {table: int(conn.execute(f"SELECT count(*) FROM dl.{table}").fetchone()[0])
                for table in tables}
    finally:
        conn.close()


def probe_unlocked(ducklake_dir: Path) -> tuple[bool, str]:
    """库没被别的进程占着才返回 True。"""
    client = ducklake_dir / "client.duckdb"
    if not client.exists():
        return False, f"找不到 {client}"
    try:
        conn = duckdb.connect(str(client))
    except Exception as exc:
        return False, str(exc)
    conn.close()
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["counts", "probe-unlocked"])
    parser.add_argument("ducklake_dir")
    parser.add_argument("--expect", metavar="FILE")
    args = parser.parse_args(argv)
    ducklake_dir = Path(args.ducklake_dir)

    if args.mode == "probe-unlocked":
        ok, reason = probe_unlocked(ducklake_dir)
        if not ok:
            print(f"DuckLake 目录仍被占用或不可用：{reason}", file=sys.stderr)
        return 0 if ok else 1

    actual = collect_counts(ducklake_dir)
    print(json.dumps(actual, ensure_ascii=False, sort_keys=True))
    if not args.expect:
        return 0

    expected = json.loads(Path(args.expect).read_text(encoding="utf-8"))
    problems = [
        f"{table}: 备份 {expected.get(table)} 行，还原后 {actual.get(table)} 行"
        for table in sorted(set(expected) | set(actual))
        if expected.get(table) != actual.get(table)
    ]
    for problem in problems:
        print(f"行数不一致：{problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
