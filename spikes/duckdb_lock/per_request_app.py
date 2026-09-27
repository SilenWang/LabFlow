#!/usr/bin/env python
"""按请求 open/close 的"应用"进程（VYB-419 / D5 补充实测）。

  pixi run python spikes/duckdb_lock/per_request_app.py --db <file> --iterations 60 \
      --hold-ms 15 --gap-ms 85 [--retry 1] [--backoff-ms 20]

每个"请求"：打开连接（拿锁）→ 执行一次写 → 挂住 hold-ms（模拟请求处理耗时）→ 关闭连接（放锁）→ 空 gap-ms。
输出每行一个 JSON 事件（app_iteration / app_summary）。
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path


def log(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return round(ordered[index], 2)


def main() -> int:
    import duckdb

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--iterations", type=int, default=60)
    parser.add_argument("--hold-ms", type=float, default=15.0, help="拿到锁之后挂住多久（模拟请求处理）")
    parser.add_argument("--gap-ms", type=float, default=85.0, help="放锁之后空闲多久")
    parser.add_argument("--retry", type=int, default=1, help="拿不到锁时的最大尝试次数")
    parser.add_argument("--backoff-ms", type=float, default=20.0, help="退避基数（指数增长）")
    parser.add_argument("--write", action="store_true", help="顺带写一行（默认只读查询）")
    args = parser.parse_args()

    db = str(Path(args.db).resolve())
    open_ms: list[float] = []
    total_ms: list[float] = []
    retries_used = 0
    failures = 0
    lock_held_ms = 0.0
    started_wall = time.perf_counter()

    for i in range(1, args.iterations + 1):
        request_started = time.perf_counter()
        conn = None
        ok = False
        error = None
        for attempt in range(1, args.retry + 1):
            attempt_started = time.perf_counter()
            try:
                conn = duckdb.connect(db)
                if args.write:
                    conn.execute("UPDATE batches SET remark = ? WHERE id = 1", [f"touch-{i}"])
                else:
                    conn.execute("SELECT count(*) FROM batches").fetchone()
                open_ms.append((time.perf_counter() - attempt_started) * 1000)
                ok = True
                if attempt > 1:
                    retries_used += 1
                break
            except Exception as exc:  # noqa: BLE001 - 实测要看原始报错
                error = f"{type(exc).__name__}: {str(exc)[:120]}"
                if attempt < args.retry:
                    time.sleep(min(args.backoff_ms * (2 ** (attempt - 1)), 250) / 1000)
        if not ok:
            failures += 1
            log("app_iteration", i=i, ok=False, attempts=args.retry, error=error)
        else:
            held_started = time.perf_counter()
            time.sleep(args.hold_ms / 1000)
            conn.close()
            lock_held_ms += (time.perf_counter() - held_started) * 1000
        total_ms.append((time.perf_counter() - request_started) * 1000)
        if args.gap_ms:
            time.sleep(args.gap_ms / 1000)

    wall_ms = (time.perf_counter() - started_wall) * 1000
    log(
        "app_summary",
        iterations=args.iterations,
        failures=failures,
        retries_used=retries_used,
        hold_ms_target=args.hold_ms,
        gap_ms_target=args.gap_ms,
        max_attempts=args.retry,
        open_ms={"mean": round(statistics.mean(open_ms), 2) if open_ms else 0, "p50": pct(open_ms, 0.5), "p95": pct(open_ms, 0.95)},
        request_ms={"mean": round(statistics.mean(total_ms), 2), "p50": pct(total_ms, 0.5), "p95": pct(total_ms, 0.95), "max": round(max(total_ms), 2)},
        lock_held_ms=round(lock_held_ms, 1),
        wall_ms=round(wall_ms, 1),
        lock_fraction=round(lock_held_ms / wall_ms, 4) if wall_ms else 0,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
