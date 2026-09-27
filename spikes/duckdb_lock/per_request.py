#!/usr/bin/env python
"""D5 补充实测：按请求 open/close（不常驻连接）时，锁到底怎么占用（VYB-419）。

  pixi run lock-per-request

回答技术组长的四个问题：
  1. 应用按请求开关时，请求间隙 MCP 能不能打开、成功率多少；
  2. MCP 持只读连接时，前端写请求还能不能进（只读是不是也双向挡）；
  3. mcp-server-motherduck 自己是常驻连接还是按查询开关；
  4. 两端都加退避重试时，能不能都不报错、代价是多少。
"""
from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPIKE = Path(__file__).resolve().parent
RUN_DIR = ROOT / ".tmp" / "duckdb_per_request"

sys.path.insert(0, str(SPIKE))
from processes import run_json  # noqa: E402

SCHEMA = """
CREATE TABLE projects(id INTEGER, name VARCHAR, created_by INTEGER, created_at VARCHAR, deleted_at VARCHAR);
CREATE TABLE batches(id INTEGER, project_id INTEGER, batch_no VARCHAR, name VARCHAR, remark VARCHAR,
                     created_by INTEGER, created_at VARCHAR, updated_at VARCHAR, deleted_at VARCHAR);
CREATE TABLE file_versions(id INTEGER, batch_id INTEGER, file_type VARCHAR, original_name VARCHAR,
                           size_bytes INTEGER, uploaded_by INTEGER, uploaded_at VARCHAR, deleted_at VARCHAR);
"""
SEED = """
INSERT INTO projects VALUES (1, 'project-a', 1, '2026-09-01', NULL);
INSERT INTO batches VALUES (1, 1, 'B-001', 'batch-a', NULL, 1, '2026-09-01', '2026-09-01', NULL);
INSERT INTO batches VALUES (2, 1, 'B-002', 'batch-b', NULL, 1, '2026-09-01', '2026-09-01', NULL);
"""


def emit(text: str = "") -> None:
    print(text, flush=True)


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))], 1)


def build_fixture() -> Path:
    import duckdb

    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    db = RUN_DIR / "labflow.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(SCHEMA)
    conn.execute(SEED)
    conn.close()
    return db


def probe_once(db: Path, retry: int = 1, backoff_ms: float = 15.0, write: bool = False, mode: str = "ro"):
    """一次"另一个进程"的开库尝试；retry>1 时按指数退避重试。"""
    import duckdb

    started = time.perf_counter()
    attempts = 0
    error = None
    for attempt in range(1, max(1, retry) + 1):
        attempts = attempt
        try:
            conn = duckdb.connect(str(db), read_only=(mode == "ro"))
            if write:
                conn.execute("UPDATE batches SET remark = '外部写' WHERE id = 2")
            else:
                conn.execute("SELECT count(*) FROM batches").fetchone()
            conn.close()
            return {"ok": True, "ms": (time.perf_counter() - started) * 1000, "attempts": attempts}
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:90]}"
            if attempt < retry:
                time.sleep(min(backoff_ms * (2 ** (attempt - 1)), 250) / 1000)
    return {"ok": False, "ms": (time.perf_counter() - started) * 1000, "attempts": attempts, "error": error}


def start_app(db: Path, log_path: Path, **kwargs) -> subprocess.Popen:
    argv = [sys.executable, str(SPIKE / "per_request_app.py"), "--db", str(db)]
    for key, value in kwargs.items():
        flag = f"--{key.replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                argv.append(flag)
        elif value is not None:
            argv += [flag, str(value)]
    log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - 交给 Popen 管
    return subprocess.Popen(argv, cwd=ROOT, stdout=log_file, stderr=subprocess.STDOUT, text=True)


def app_events(log_path: Path) -> list[dict]:
    events = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def app_summary(log_path: Path) -> dict:
    for event in app_events(log_path):
        if event.get("event") == "app_summary":
            return event
    return {}


def measure_while_app_runs(db: Path, log_path: Path, app_kwargs: dict, interval_ms: float,
                           retry: int = 1, backoff_ms: float = 15.0, write: bool = False):
    """起一个按请求开关的应用子进程，同时在"另一个进程"里按固定节奏开库，统计成功率。"""
    proc = start_app(db, log_path, **app_kwargs)
    results = []
    started = time.perf_counter()
    while proc.poll() is None and time.perf_counter() - started < 60:
        results.append(probe_once(db, retry=retry, backoff_ms=backoff_ms, write=write))
        time.sleep(interval_ms / 1000)
    proc.wait(timeout=30)
    return proc.returncode, results, app_summary(log_path)


def summarize_probe(results: list[dict]) -> dict:
    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    retried = [r for r in results if r["attempts"] > 1]
    return {
        "attempts": len(results),
        "ok": len(ok),
        "failed": len(failed),
        "success_rate": round(len(ok) / len(results), 4) if results else 0,
        "retried": len(retried),
        "ms_p50": pct([r["ms"] for r in results], 0.5),
        "ms_p95": pct([r["ms"] for r in results], 0.95),
        "ms_max": round(max([r["ms"] for r in results]), 1) if results else 0,
        "sample_error": failed[0]["error"] if failed else None,
    }


def show_app(tag: str, summary: dict) -> None:
    if not summary:
        emit(f"{tag} 应用：无统计")
        return
    emit(
        f"{tag} 应用：{summary['iterations']} 个请求，失败 {summary['failures']}，重试 {summary['retries_used']} 次；"
        f"单次 open {summary['open_ms']['mean']}ms(p95 {summary['open_ms']['p95']})，"
        f"请求耗时 {summary['request_ms']['mean']}ms(p95 {summary['request_ms']['p95']}, max {summary['request_ms']['max']})；"
        f"锁占用 {summary['lock_held_ms']}ms / {summary['wall_ms']}ms = {summary['lock_fraction'] * 100:.1f}%"
    )


def section_zero(db: Path) -> None:
    emit("")
    emit("== 0. 每请求 open/close 的开销（SQLAlchemy + duckdb-engine，贴近应用写法） ==")
    emit("   （同时看本进程有没有把库文件 fd 一直握着——这就是「锁是否持续被占」的直接证据）")
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool, StaticPool

    def measure_fresh(engine, sql: str, n: int = 30) -> list[float]:
        samples = []
        for _ in range(n):
            t = time.perf_counter()
            conn = engine.connect()
            conn.execute(text(sql))
            conn.close()
            samples.append((time.perf_counter() - t) * 1000)
        return samples

    def measure_persistent(engine, sql: str, n: int = 30) -> list[float]:
        conn = engine.connect()
        samples = []
        for _ in range(n):
            t = time.perf_counter()
            conn.execute(text(sql))
            samples.append((time.perf_counter() - t) * 1000)
        conn.close()
        return samples

    null_engine = create_engine(f"duckdb:///{db}", poolclass=NullPool)
    samples = measure_fresh(null_engine, "SELECT count(*) FROM batches")
    emit(
        f"0.a NullPool（每请求新开连接、用完即关）：均值 {statistics.mean(samples):.1f}ms，"
        f"p50 {pct(samples, 0.5)}ms，p95 {pct(samples, 0.95)}ms；库文件 fd 仍持有 = {fd_open_now(db)}"
    )
    null_engine.dispose()

    static_engine = create_engine(f"duckdb:///{db}", poolclass=StaticPool)
    samples = measure_persistent(static_engine, "SELECT count(*) FROM batches")
    emit(
        f"0.b 常驻连接（StaticPool，SQLAlchemy 默认池的形状）：均值 {statistics.mean(samples):.2f}ms，"
        f"p95 {pct(samples, 0.95)}ms；库文件 fd 仍持有 = {fd_open_now(db)}"
    )
    static_engine.dispose()


def fd_open_now(db: Path) -> bool:
    db_name = db.name
    for fd in Path("/proc/self/fd").iterdir():
        try:
            if db_name in str(fd.resolve()):
                return True
        except OSError:
            continue
    return False


def section_one(db: Path) -> None:
    emit("")
    emit("== 1. 应用按请求 open/close（无重试）：请求间隙，第二个进程能不能打开 ==")
    rc, results, summary = measure_while_app_runs(
        db,
        RUN_DIR / "app_no_retry.log",
        {"iterations": 80, "hold_ms": 15, "gap_ms": 85, "write": True},
        interval_ms=20,
    )
    show_app("1", summary)
    stats = summarize_probe(results)
    emit(
        f"1 第二个进程（只读开库，不重试）{stats['attempts']} 次尝试：成功 {stats['ok']}，失败 {stats['failed']}，"
        f"成功率 {stats['success_rate'] * 100:.1f}%；耗时 p50 {stats['ms_p50']}ms / p95 {stats['ms_p95']}ms"
    )
    if stats["sample_error"]:
        emit(f"  失败样例：{stats['sample_error']}")


def section_two(db: Path) -> None:
    emit("")
    emit("== 2. 官方 MCP 真跑：应用三种形态下同一会话里的查询成功率 ==")

    def mcp(*extra: str) -> list[dict]:
        return run_json([sys.executable, str(SPIKE / "mcp_probe.py"), "--db-path", str(db), *extra], timeout=300)

    def line(tag: str, events: list[dict]) -> None:
        calls = [e for e in events if e.get("event") == "mcp_tool_call_query"]
        ok = [c for c in calls if c.get("ok")]
        ms = [c["ms"] for c in calls]
        emit(f"2 {tag}：MCP 查了 {len(calls)} 次，成功 {len(ok)} 次（{len(ok) / len(calls) * 100 if calls else 0:.0f}%），耗时 p50 {pct(ms, 0.5)}ms / max {round(max(ms), 1) if ms else 0}ms")
        if len(ok) != len(calls) and calls:
            failed = [c for c in calls if not c.get("ok")][0]
            sample = failed.get("result") or failed.get("result_preview") or failed.get("error")
            emit(f"   失败样例：{str(sample)[:160]}")

    emit("2.a 应用空闲（没有任何进程持锁）——基线")
    line("应用空闲", mcp("--queries", "5"))

    emit("2.b 应用按请求 open/close 中（每请求持锁约 15ms、间隙约 85ms）")
    proc = start_app(db, RUN_DIR / "app_for_mcp.log", iterations=120, hold_ms=15, gap_ms=85, write=False)
    events = mcp("--queries", "20")
    proc.wait(timeout=60)
    show_app("2.b", app_summary(RUN_DIR / "app_for_mcp.log"))
    line("应用按请求开关", events)

    emit("2.c 应用常驻连接（当前形状，baseline）")
    from processes import Holder

    holder = Holder("duckdb", db, mode="rw")
    holder.wait_event()
    line("应用常驻", mcp("--queries", "3"))
    holder.stop()


def section_three(db: Path) -> None:
    emit("")
    emit("== 3. 反向：MCP 持只读连接时，前端（按请求 open/close）的写请求还能不能进 ==")

    def run_mcp_background(*extra: str) -> tuple[subprocess.Popen, Path]:
        log_path = RUN_DIR / f"mcp_bg_{int(time.time() * 1000)}.log"
        log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
        argv = [sys.executable, str(SPIKE / "mcp_probe.py"), "--db-path", str(db), *extra]
        return subprocess.Popen(argv, cwd=ROOT, stdout=log_file, stderr=subprocess.STDOUT), log_path

    for tag, extra in (
        ("3.a MCP --no-ephemeral-connections（常驻只读连接）", ["--no-ephemeral", "--queries", "1", "--hold-ms", "6000"]),
        ("3.b MCP 默认（ephemeral，查完即放）", ["--queries", "1", "--hold-ms", "6000"]),
    ):
        emit(tag)
        proc, log_path = run_mcp_background(*extra)
        time.sleep(3.5)  # 等它 initialize + 查询完（此时连接状态已确定）并处于 hold 期
        writes = [probe_once(db, write=True, mode="rw") for _ in range(3)]
        time.sleep(0.5)
        results = [
            f"{'成功' if w['ok'] else '失败'} {w['ms']:.0f}ms" + ("" if w["ok"] else f"（{w['error']}）")
            for w in writes
        ]
        emit(f"    MCP 存活期间前端 3 次写请求：{results}")
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1)
        after = probe_once(db, write=True, mode="rw")
        emit(f"    MCP 退出后前端写请求：{'成功' if after['ok'] else '失败'} {after['ms']:.0f}ms")


def section_four(db: Path) -> None:
    emit("")
    emit("== 4. mcp-server-motherduck 自己是常驻连接还是按查询开关 ==")
    for tag, extra in (
        ("4.a 默认（本地只读文件）", []),
        ("4.b --no-ephemeral-connections", ["--no-ephemeral"]),
        ("4.c --read-write", ["--read-write"]),
    ):
        events = run_json(
            [
                sys.executable,
                str(SPIKE / "mcp_probe.py"),
                "--db-path",
                str(db),
                *extra,
                "--queries",
                "2",
                "--fd-check",
                str(db),
                "--post-probe-db",
                str(db),
            ],
            timeout=300,
        )
        fd = next((e for e in events if e.get("event") == "mcp_fd_check"), {})
        post = next((e for e in events if e.get("event") == "mcp_post_probe"), {})
        emit(
            f"{tag}：查询后 MCP 进程仍持有库文件句柄 = {fd.get('fd_open')}"
            f"{'（' + str(fd.get('matches')) + '）' if fd.get('fd_open') else ''}；"
            f"此时另一个进程开库 = {'成功' if post.get('ok') else '失败'}"
        )


def section_five(db: Path) -> None:
    emit("")
    emit("== 5. 两端都加退避重试：能不能都不报错、代价多少 ==")
    rc, results, summary = measure_while_app_runs(
        db,
        RUN_DIR / "app_with_retry.log",
        {"iterations": 60, "hold_ms": 15, "gap_ms": 60, "retry": 25, "backoff_ms": 20, "write": True},
        interval_ms=15,
        retry=25,
        backoff_ms=15,
        write=False,
    )
    show_app("5", summary)
    stats = summarize_probe(results)
    emit(
        f"5 第二个进程（带退避重试，最多 25 次）{stats['attempts']} 次尝试：失败 {stats['failed']}，"
        f"重试过 {stats['retried']} 次；耗时 p50 {stats['ms_p50']}ms / p95 {stats['ms_p95']}ms / max {stats['ms_max']}ms"
    )
    emit("  → 两侧都退避重试时能收敛到 0 报错，代价是撞锁的那次请求要等到对面放锁（延迟抖动）。")


def main() -> int:
    import duckdb

    emit("# D5 补充实测：按请求 open/close 时的锁占用（VYB-419）")
    emit(f"环境：python {sys.version.split()[0]} / duckdb {duckdb.__version__}")
    db = build_fixture()
    emit(f"夹具：{db}（projects / batches / file_versions，2 行 batches）")
    section_zero(db)
    section_one(db)
    section_two(db)
    section_three(db)
    section_four(db)
    section_five(db)
    emit("")
    emit("# 实测结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
