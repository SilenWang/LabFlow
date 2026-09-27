#!/usr/bin/env python
"""一次性探测进程：尝试打开/查询别人正在用的 duckdb 文件（VYB-419 / D5 实测用）。

  pixi run python spikes/duckdb_lock/probe_duckdb.py --db <file> --mode rw|ro|attach-ro|no-lock
  pixi run python spikes/duckdb_lock/probe_duckdb.py --db <file> --mode ro --retry 5 --interval 1

输出每行一个 JSON 事件；退出码 0 = 全部尝试成功，1 = 至少一次失败。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def log(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def open_and_query(db: str, mode: str, sql: str):
    import duckdb

    started = time.perf_counter()
    try:
        if mode == "rw":
            conn = duckdb.connect(db)
        elif mode == "ro":
            conn = duckdb.connect(db, read_only=True)
        elif mode == "no-lock":
            conn = duckdb.connect(db, config={"lock_configuration": False})
        elif mode == "attach-ro":
            # 模拟"另开一个内存库、ATTACH 只读挂载目标文件"这条常见绕法
            conn = duckdb.connect(":memory:")
            conn.execute(f"ATTACH '{db}' AS target (READ_ONLY)")
            conn.execute("USE target")
        else:  # pragma: no cover - argparse 已限制
            raise ValueError(mode)
    except Exception as exc:  # noqa: BLE001 - 实测就是要看原始报错
        return {
            "ok": False,
            "stage": "open",
            "type": type(exc).__name__,
            "message": str(exc),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }

    open_ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        rows = conn.execute(sql).fetchall()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "stage": "query",
            "type": type(exc).__name__,
            "message": str(exc),
            "ms": round((time.perf_counter() - started) * 1000, 1),
            "open_ms": open_ms,
        }
    finally:
        conn.close()
    return {
        "ok": True,
        "stage": "query",
        "rows": [list(r) for r in rows],
        "ms": round((time.perf_counter() - started) * 1000, 1),
        "open_ms": open_ms,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--mode", choices=["rw", "ro", "no-lock", "attach-ro"], default="ro")
    parser.add_argument("--sql", default="SELECT count(*) FROM batches")
    parser.add_argument("--retry", type=int, default=1, help="尝试次数（>1 时用于验证是否重试能等到锁）")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    db = str(Path(args.db).resolve())
    log("probe_start", label=args.label, db=db, mode=args.mode, sql=args.sql, retry=args.retry)

    any_failed = False
    for attempt in range(1, args.retry + 1):
        result = open_and_query(db, args.mode, args.sql)
        any_failed = any_failed or not result["ok"]
        log("probe_attempt", attempt=attempt, **result)
        if result["ok"]:
            break
        if attempt < args.retry:
            time.sleep(args.interval)

    log("probe_done", label=args.label, any_failed=any_failed)
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
