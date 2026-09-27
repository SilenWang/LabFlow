#!/usr/bin/env python
"""长驻进程：模拟"应用"持有数据库连接（VYB-419 / D5 实测用）。

连接的打开与关闭由 stdin 指令控制，用来实测"锁在什么时刻释放"。

  pixi run python spikes/duckdb_lock/holder.py --kind duckdb --db <file> [--mode rw|ro]
  pixi run python spikes/duckdb_lock/holder.py --kind sqlite --db <file> [--busy-timeout 5]

指令（每行一条）：
  open 关闭后重新打开连接 / close 关闭连接但进程存活 / begin 开写事务并执行 --begin-sql
  commit / ping / exit

输出：每行一个 JSON 事件（ready / opened / closed / began / committed / pong / bye / error）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def log(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["duckdb", "sqlite"], required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--mode", choices=["rw", "ro"], default="rw")
    parser.add_argument("--busy-timeout", type=float, default=5.0)
    parser.add_argument("--begin-sql", default=None, help="begin 指令要执行的写语句")
    args = parser.parse_args()

    db = str(Path(args.db).resolve())
    conn = None

    def connect():
        if args.kind == "duckdb":
            import duckdb

            return duckdb.connect(db, read_only=(args.mode == "ro"))
        import sqlite3

        c = sqlite3.connect(db, timeout=args.busy_timeout, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(f"PRAGMA busy_timeout={int(args.busy_timeout * 1000)}")
        return c

    def close():
        nonlocal conn
        if conn is not None:
            conn.close()
            conn = None
            log("closed", connection_open=False)

    try:
        conn = connect()
    except Exception as exc:  # noqa: BLE001 - 实测就是要看原始报错
        log("error", stage="open", type=type(exc).__name__, message=str(exc))
        return 1

    log("ready", pid=os.getpid(), db=db, kind=args.kind, mode=args.mode, connection_open=True)

    for raw in sys.stdin:
        cmd = raw.strip()
        if not cmd:
            continue
        try:
            if cmd == "close":
                close()
            elif cmd == "open":
                if conn is None:
                    conn = connect()
                    log("opened", connection_open=True)
            elif cmd == "begin":
                conn.execute("BEGIN TRANSACTION")
                if args.begin_sql:
                    conn.execute(args.begin_sql)
                log("began", connection_open=True)
            elif cmd == "commit":
                conn.execute("COMMIT")
                log("committed")
            elif cmd == "ping":
                conn.execute("SELECT 1")
                log("pong", connection_open=conn is not None)
            elif cmd == "exit":
                break
            else:
                log("error", stage="command", message=f"unknown command {cmd!r}")
        except Exception as exc:  # noqa: BLE001
            log("error", stage=cmd, type=type(exc).__name__, message=str(exc))

    try:
        close()
    finally:
        log("bye", connection_open=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
