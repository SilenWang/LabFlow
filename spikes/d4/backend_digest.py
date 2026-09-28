"""D4 记录用：独立读一遍某个后端，输出 4 表的行数与内容校验和。

用法::

    pixi run python spikes/d4/backend_digest.py sqlite   data/labflow.db
    pixi run python spikes/d4/backend_digest.py ducklake data/ducklake

刻意不复用 server/ 或迁移脚本里的比对函数：sqlite 走 stdlib sqlite3，DuckLake 走裸
duckdb attach（只读），两侧用同一套长度前缀 sha256 编码，输出可比。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import duckdb

TABLES = ("users", "projects", "batches", "file_versions")


def _attach_ducklake(dir_path):
    path = Path(dir_path).resolve()
    conn = duckdb.connect(":memory:")
    conn.execute("LOAD ducklake; LOAD sqlite")
    conn.execute(
        f"ATTACH 'ducklake:sqlite:{path / 'catalog.sqlite'}' AS dl "
        f"(DATA_PATH '{path / 'data'}', OVERRIDE_DATA_PATH true, READ_ONLY)"
    )
    return conn


def read_sqlite(db_path, table):
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]
        identifiers = ", ".join(f'"{c}"' for c in columns)
        rows = conn.execute(f'SELECT {identifiers} FROM "{table}" ORDER BY id').fetchall()
    finally:
        conn.close()
    return columns, rows


def read_ducklake(conn, table):
    columns = [r[0] for r in conn.execute(f"DESCRIBE dl.main.{table}").fetchall()]
    rows = conn.execute(f"SELECT * FROM dl.{table} ORDER BY id").fetchall()
    return columns, rows


def _cell(value):
    if value is None:
        return b"\x00NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return b"\x01HEX" + bytes(value).hex().encode("ascii")
    return b"\x02TEXT" + str(value).encode("utf-8")


def _digest(columns, rows):
    digest = hashlib.sha256()
    for column in columns:
        digest.update(column.encode("utf-8"))
    for row in rows:
        for value in row:
            payload = _cell(value)
            digest.update(str(len(payload)).encode("ascii"))
            digest.update(b":")
            digest.update(payload)
    return digest.hexdigest()


def main(argv):
    backend, target = argv[1], argv[2]
    conn = _attach_ducklake(target) if backend == "ducklake" else None
    try:
        tables = {}
        for table in TABLES:
            read = read_ducklake(conn, table) if conn else read_sqlite(target, table)
            columns, rows = read
            tables[table] = {"rows": len(rows), "checksum": _digest(columns, rows)}
    finally:
        if conn:
            conn.close()
    print(json.dumps({"backend": backend, "target": str(target), "tables": tables},
                     ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
