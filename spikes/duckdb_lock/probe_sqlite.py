#!/usr/bin/env python
"""一次性探测进程：sqlite 对照（WAL + busy_timeout）（VYB-419 / D5 实测用）。

  pixi run python spikes/duckdb_lock/probe_sqlite.py --db <file> --mode read  --timeout 5
  pixi run python spikes/duckdb_lock/probe_sqlite.py --db <file> --mode write --timeout 5

输出每行一个 JSON 事件；退出码 0 = 成功，1 = 失败（含拿不到锁超时）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path


def log(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--mode", choices=["read", "write"], default="read")
    parser.add_argument("--timeout", type=float, default=5.0, help="busy_timeout（秒）")
    parser.add_argument("--sql", default="SELECT count(*) FROM batches")
    parser.add_argument("--value", default="probe-write")
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    db = str(Path(args.db).resolve())
    started = time.perf_counter()
    try:
        conn = sqlite3.connect(db, timeout=args.timeout, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=%d" % int(args.timeout * 1000))
        if args.mode == "read":
            rows = conn.execute(args.sql).fetchall()
        else:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO batches (project_id, batch_no, name, created_by, created_at, updated_at) "
                "VALUES (1, ?, ?, 1, 'ts', 'ts')",
                (f"p-{args.value}", args.value),
            )
            conn.execute("COMMIT")
            rows = [["committed"]]
        conn.close()
    except Exception as exc:  # noqa: BLE001 - 实测就是要看原始报错
        log(
            "probe_attempt",
            label=args.label,
            mode=args.mode,
            timeout_s=args.timeout,
            ok=False,
            type=type(exc).__name__,
            message=str(exc),
            ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return 1

    log(
        "probe_attempt",
        label=args.label,
        mode=args.mode,
        timeout_s=args.timeout,
        ok=True,
        rows=[list(r) for r in rows],
        ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
