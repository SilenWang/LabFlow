#!/usr/bin/env python
"""D5 独立复核：mcp-server-motherduck × DuckDB 独占锁（含 sqlite 对照）。

  pixi run lock-spike            # 全量实测，stdout 即留档日志

覆盖：
  A. 应用持有常驻连接时，第二个进程开同一 duckdb 文件（rw / read_only / ATTACH READ_ONLY /
     lock_configuration=false / 重试等待）分别怎样，以及锁在什么时刻释放。
  B. 第一个进程用只读打开时，第二个进程能读 / 能写吗。
  C. sqlite 对照（WAL + busy_timeout）：第二个进程能读吗、能写吗、排队能不能等到锁。
  D. 官方本地包 mcp-server-motherduck 真跑 stdio 会话（应用持锁 / 空闲 / 反向持锁 / ephemeral）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from processes import Holder, run_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = ROOT / ".tmp" / "duckdb_lock_run"
SPIKE = Path(__file__).resolve().parent

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
"""


def emit(text: str = "") -> None:
    print(text, flush=True)


def show(prefix: str, events: list[dict]) -> None:
    label = ""
    for event in events:
        name = event.get("event")
        if name == "probe_start":
            label = event.get("label") or ""
            continue
        if name in {"mcp_start", "exit_code"}:
            continue
        if name == "probe_done":
            emit(f"{prefix}  └ 结果：{'有失败' if event['any_failed'] else '全部成功'}")
        elif name == "mcp_done":
            emit(f"{prefix}  └ 结果：{'成功' if event['ok'] else '失败'}")
        elif name == "mcp_exit":
            if event.get("stderr_tail"):
                emit(f"{prefix}  [stderr] {json.dumps(event['stderr_tail'], ensure_ascii=False)}")
        elif name == "probe_attempt":
            tag = label or event.get("mode")
            if event.get("ok"):
                detail = f"成功 {event['ms']}ms"
                if "open_ms" in event:
                    detail += f"（开库 {event['open_ms']}ms）"
                rows = event.get("rows")
                if rows and event.get("mode") != "write":
                    detail += f" 结果={rows[:1]}"
                emit(f"{prefix}  [{tag}] {detail}")
            else:
                stage = f"({event['stage']})" if event.get("stage") else ""
                emit(
                    f"{prefix}  [{tag}] 失败{stage} "
                    f"{event['ms']}ms：{event['type']}: {event['message']}"
                )
        elif name.startswith("mcp_"):
            fields = {k: v for k, v in event.items() if k not in {"event", "label", "result_preview", "stage", "ms"}}
            if "ms" in event:
                fields["ms"] = event["ms"]
            if "result_preview" in event:
                fields["result"] = event["result_preview"]
            if event.get("ok") is not None and name == "mcp_post_probe":
                fields = {
                    "ok": event["ok"],
                    "ms": event.get("ms"),
                    "error": event.get("message") if not event["ok"] else None,
                }
            emit(f"{prefix}  <<{name}>> {json.dumps(fields, ensure_ascii=False)}")
        elif name == "stderr":
            emit(f"{prefix}  [stderr] {event['line']}")
        elif name == "raw":
            emit(f"{prefix}  [raw] {event['line']}")
        else:
            emit(f"{prefix}  {json.dumps(event, ensure_ascii=False)}")


def build_fixtures() -> tuple[Path, Path]:
    import duckdb
    import sqlite3

    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    duck = RUN_DIR / "labflow.duckdb"
    conn = duckdb.connect(str(duck))
    conn.execute(SCHEMA)
    conn.execute(SEED)
    conn.close()

    sq = RUN_DIR / "labflow.sqlite"
    conn = sqlite3.connect(str(sq))
    conn.executescript(SCHEMA)
    conn.executescript(SEED)
    conn.commit()
    conn.close()
    return duck, sq


def probe(argv_extra: list[str], label: str) -> list[dict]:
    return run_json([sys.executable, str(SPIKE / "probe_duckdb.py"), *argv_extra, "--label", label])


def test_a(duck: Path) -> None:
    emit("")
    emit("== A. DuckDB：应用持常驻连接时，第二个进程开同一文件 ==")
    holder = Holder("duckdb", duck, mode="rw")
    ready = holder.wait_event()
    emit(f"A1 应用进程（rw 常驻连接，空闲无事务）：pid={ready.get('pid')} connection_open={ready.get('connection_open')}")

    for mode in ["rw", "ro", "attach-ro", "no-lock"]:
        show(f"A2.{mode}", probe(["--db", str(duck), "--mode", mode], f"应用持锁/{mode}"))

    show(
        "A2.retry",
        probe(["--db", str(duck), "--mode", "ro", "--retry", "5", "--interval", "1"], "应用持锁/ro重试5次每秒1次"),
    )

    emit("A3 应用关闭连接（进程继续存活，等价于「按需 open/close」）")
    emit(f"  holder 事件：{json.dumps(holder.command('close'), ensure_ascii=False)}")
    for mode in ["ro", "rw"]:
        show(f"A3.{mode}", probe(["--db", str(duck), "--mode", mode], f"应用已关连接/{mode}"))

    emit("A4 应用重新打开连接（锁随即被再次独占）")
    emit(f"  holder 事件：{json.dumps(holder.command('open'), ensure_ascii=False)}")
    show("A4.ro", probe(["--db", str(duck), "--mode", "ro"], "应用重开连接/ro"))

    emit("A5 应用进程退出（锁随进程退出释放）")
    holder.stop()
    show("A5.ro", probe(["--db", str(duck), "--mode", "ro"], "应用已退出/ro"))

    emit("A6 「排队等待」验证：应用 4 秒后放锁，第二个进程每秒重试一次")
    waiter = Holder("duckdb", duck, mode="rw")
    waiter.wait_event()
    retry = subprocess.Popen(
        [
            sys.executable,
            str(SPIKE / "probe_duckdb.py"),
            "--db",
            str(duck),
            "--mode",
            "ro",
            "--retry",
            "10",
            "--interval",
            "1",
            "--label",
            "排队重试(应用4秒后放锁)",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(4)
    emit(f"  holder 事件（放锁）：{json.dumps(waiter.command('close'), ensure_ascii=False)}")
    out, _ = retry.communicate(timeout=60)
    show("A6", [json.loads(line) for line in out.splitlines() if line.strip()])
    waiter.stop()


def test_b(duck: Path) -> None:
    emit("")
    emit("== B. DuckDB：第一个进程只读打开时，第二个进程能读 / 能写吗 ==")
    holder = Holder("duckdb", duck, mode="ro")
    ready = holder.wait_event()
    emit(f"B1 第一个进程只读打开：pid={ready.get('pid')} mode={ready.get('mode')}")
    show("B2.ro", probe(["--db", str(duck), "--mode", "ro"], "只读持锁者/第二个只读"))
    show(
        "B3.rw",
        probe(
            ["--db", str(duck), "--mode", "rw", "--sql", "INSERT INTO projects VALUES (99, 'x', 1, 'ts', NULL)"],
            "只读持锁者/第二个读写写入",
        ),
    )
    holder.stop()


def test_c(sq: Path) -> None:
    emit("")
    emit("== C. sqlite 对照（WAL + busy_timeout）：第二个进程能读 / 能写吗 ==")
    insert = "INSERT INTO batches (project_id, batch_no, name, created_by, created_at, updated_at) VALUES (1, 'holder-1', 'holder', 1, 'ts', 'ts')"
    writer = Holder("sqlite", sq, busy_timeout=5.0, begin_sql=insert)
    ready = writer.wait_event()
    emit(f"C1 应用进程（sqlite，WAL，busy_timeout=5s）：pid={ready.get('pid')}")
    emit(f"  holder 事件：{json.dumps(writer.command('begin'), ensure_ascii=False)}   <- 应用持有一个未提交的写事务（已 INSERT）")

    show("C2", run_json([sys.executable, str(SPIKE / "probe_sqlite.py"), "--db", str(sq), "--mode", "read", "--timeout", "5", "--label", "应用持写事务/第二个读"]))

    emit("C3 应用 3 秒后提交；第二个进程带 5s busy_timeout 去写（预期：排队等到，成功）")
    timer = threading.Timer(3.0, lambda: writer.command("commit"))
    timer.start()
    show(
        "C3",
        run_json(
            [sys.executable, str(SPIKE / "probe_sqlite.py"), "--db", str(sq), "--mode", "write", "--timeout", "5", "--value", "c3", "--label", "应用3s后提交/第二个写(等5s)"]
        ),
    )
    timer.join()

    emit("C4 应用持有写事务不放；第二个进程 busy_timeout=1s 去写（预期：排队等到超时才失败）")
    emit(f"  holder 事件：{json.dumps(writer.command('begin'), ensure_ascii=False)}")
    show(
        "C4",
        run_json(
            [sys.executable, str(SPIKE / "probe_sqlite.py"), "--db", str(sq), "--mode", "write", "--timeout", "1", "--value", "c4", "--label", "应用持锁不放/第二个写(等1s)"]
        ),
    )

    emit("C5 同上，但 busy_timeout=0（不排队）：用来证明「等待」是 busy_timeout 给的，不是自动的")
    show(
        "C5",
        run_json(
            [sys.executable, str(SPIKE / "probe_sqlite.py"), "--db", str(sq), "--mode", "write", "--timeout", "0", "--value", "c5", "--label", "应用持锁不放/第二个写(busy_timeout=0)"]
        ),
    )
    emit(f"  holder 事件：{json.dumps(writer.command('commit'), ensure_ascii=False)}")
    writer.stop()


def test_d(duck: Path) -> None:
    emit("")
    emit("== D. 官方本地包 mcp-server-motherduck 实测（stdio，本地文件，无 token、不联网） ==")

    def mcp(*extra: str, label: str) -> list[dict]:
        return run_json([sys.executable, str(SPIKE / "mcp_probe.py"), "--db-path", str(duck), *extra, "--label", label], timeout=300)

    emit("D1 应用不在跑：MCP 直连本地文件（基线，证明本地包可用）")
    show("D1", mcp(label="应用空闲/MCP默认只读"))

    emit("D2 应用在跑（rw 常驻连接）：MCP 默认（只读）")
    holder = Holder("duckdb", duck, mode="rw")
    holder.wait_event()
    show("D2", mcp(label="应用持锁/MCP默认只读"))

    emit("D3 应用在跑：MCP --read-write")
    show("D3", mcp("--read-write", label="应用持锁/MCP读写"))

    emit("D4 应用在跑：MCP --ephemeral-connections（每次查询新开连接）")
    show("D4", mcp("--ephemeral", label="应用持锁/MCP-ephemeral"))

    emit("D5 应用进程退出（锁释放）")
    holder.stop()
    show("D5", mcp(label="应用已退出/MCP默认只读"))

    emit("D6 反向：应用空闲，MCP 默认（读只 + ephemeral）查过一次、进程仍存活时，第三个进程能否独占开库")
    show("D6", mcp("--post-probe-db", str(duck), label="应用空闲/MCP默认(ephemeral)存活期试开"))

    emit("D7 反向：应用空闲，MCP --read-write（常驻读写连接）查过一次后，第三个进程能否开库")
    show("D7", mcp("--read-write", "--post-probe-db", str(duck), label="应用空闲/MCP读写存活期试开"))

    emit("D8 反向：应用空闲，MCP --no-ephemeral-connections（常驻只读连接）查过一次后，第三个进程能否开库")
    show("D8", mcp("--no-ephemeral", "--post-probe-db", str(duck), label="应用空闲/MCP常驻只读存活期试开"))

    emit("D9 :memory: 库（MCP 默认档，证明不需要任何云 / token）")
    show(
        "D9",
        run_json(
            [
                sys.executable,
                str(SPIKE / "mcp_probe.py"),
                "--db-path",
                ":memory:",
                "--read-write",
                "--sql",
                "SELECT 42 AS n",
                "--label",
                "内存库",
            ],
            timeout=300,
        ),
    )


def main() -> int:
    import duckdb

    emit("# D5 独立复核实测日志（VYB-419）")
    emit(f"环境：python {sys.version.split()[0]} / duckdb {duckdb.__version__} / cwd={ROOT}")
    emit(f"mcp-server-motherduck: {shutil.which('mcp-server-motherduck')}")
    duck, sq = build_fixtures()
    emit(f"夹具：{duck.name} / {sq.name}（projects / batches / file_versions 三表，含各 1 行）")

    test_a(duck)
    test_b(duck)
    test_c(sq)
    test_d(duck)

    emit("")
    emit("# 实测结束")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
