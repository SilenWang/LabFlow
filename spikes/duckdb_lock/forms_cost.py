#!/usr/bin/env python
"""三种"绕锁形态"的成本与新鲜度实测（VYB-419 / D5）。

  pixi run lock-forms

(i)  MCP 只读导出的快照：导出耗时 / 体积 / 谁能触发 / 滞后窗口
(ii) 应用侧 HTTP 出口：复用现有接口的调用成本与改动面
(iii) 应用改成按需 open/close：单次开销、锁是否真的释放、并发打架的后果
"""
from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPIKE = Path(__file__).resolve().parent
RUN_DIR = ROOT / ".tmp" / "duckdb_forms"

sys.path.insert(0, str(SPIKE))
sys.path.insert(0, str(ROOT))
from processes import Holder, run_json  # noqa: E402

PROJECTS = 50
BATCHES = 20_000
FILES = 60_000

SCHEMA = """
CREATE TABLE projects(id INTEGER, name VARCHAR, created_by INTEGER, created_at VARCHAR, deleted_at VARCHAR);
CREATE TABLE batches(id INTEGER, project_id INTEGER, batch_no VARCHAR, name VARCHAR, remark VARCHAR,
                     created_by INTEGER, created_at VARCHAR, updated_at VARCHAR, deleted_at VARCHAR);
CREATE TABLE file_versions(id INTEGER, batch_id INTEGER, file_type VARCHAR, original_name VARCHAR,
                           size_bytes INTEGER, uploaded_by INTEGER, uploaded_at VARCHAR, deleted_at VARCHAR);
"""


def emit(text: str = "") -> None:
    print(text, flush=True)


def human_bytes(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024


def build_live_db() -> Path:
    import duckdb

    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    live = RUN_DIR / "labflow.duckdb"
    conn = duckdb.connect(str(live))
    conn.execute(SCHEMA)
    conn.execute(
        "INSERT INTO projects SELECT i, 'project-' || i, 1, '2026-09-01', NULL FROM range(1, ?) t(i)",
        [PROJECTS + 1],
    )
    conn.execute(
        """INSERT INTO batches
           SELECT i, 1 + (i % ?), 'B-' || i, 'batch-' || i, 'remark ' || i, 1, '2026-09-01', '2026-09-01', NULL
           FROM range(1, ?) t(i)""",
        [PROJECTS, BATCHES + 1],
    )
    conn.execute(
        """INSERT INTO file_versions
           SELECT i, 1 + (i % ?), 'data_summary', 'file-' || i || '.xlsx', 1024 * (i % 500), 1, '2026-09-01', NULL
           FROM range(1, ?) t(i)""",
        [BATCHES, FILES + 1],
    )
    conn.close()
    return live


def section_one(live: Path) -> None:
    import duckdb

    emit("")
    emit("== (i) 快照导出：耗时 / 体积 / 谁能触发 / 滞后窗口 ==")
    counts = {}
    conn = duckdb.connect(str(live))
    for table in ("projects", "batches", "file_versions"):
        counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    emit(f"活库规模：{counts}（projects / batches / file_versions）")

    # a) 应用进程内、用现有连接导出 parquet 目录（EXPORT DATABASE）
    export_dir = RUN_DIR / "snapshot_export"
    t = time.perf_counter()
    conn.execute(f"EXPORT DATABASE '{export_dir}' (FORMAT PARQUET)")
    export_ms = (time.perf_counter() - t) * 1000
    export_size = sum(f.stat().st_size for f in export_dir.rglob("*") if f.is_file())
    emit(f"(i).a 应用进程内 EXPORT DATABASE → parquet 目录：{export_ms:.0f}ms，{human_bytes(export_size)}")

    # b) 应用进程内、导出一份只读 duckdb 文件（MCP 要读的形态）
    snap = RUN_DIR / "snapshot.duckdb"
    t = time.perf_counter()
    conn.execute(f"ATTACH '{snap}' AS snap")
    for table in ("projects", "batches", "file_versions"):
        conn.execute(f"CREATE TABLE snap.{table} AS SELECT * FROM {table}")
    conn.execute("DETACH snap")
    snap_ms = (time.perf_counter() - t) * 1000
    emit(f"(i).b 应用进程内 ATTACH + CREATE TABLE AS → 只读快照文件：{snap_ms:.0f}ms，{human_bytes(snap.stat().st_size)}")

    # c) 第二个进程（cron / MCP 侧）能不能自己导：应用此刻仍持有连接
    probe = subprocess.run(
        [
            sys.executable,
            str(SPIKE / "probe_duckdb.py"),
            "--db",
            str(live),
            "--mode",
            "rw",
            "--label",
            "外部进程想自己导快照",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    for line in probe.stdout.splitlines():
        event = json.loads(line)
        if event.get("event") == "probe_attempt":
            emit(
                f"(i).c 第二个进程（不先停应用）自己开库导出：{'成功' if event['ok'] else '失败'} "
                f"{event['ms']}ms {event.get('type', '')}: {str(event.get('message', ''))[:120]}"
            )

    conn.close()

    # d) 快照本身能被 MCP 独立打开吗
    probe = subprocess.run(
        [
            sys.executable,
            str(SPIKE / "probe_duckdb.py"),
            "--db",
            str(snap),
            "--mode",
            "ro",
            "--sql",
            "SELECT count(*) FROM batches",
            "--label",
            "MCP 读快照",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    for line in probe.stdout.splitlines():
        event = json.loads(line)
        if event.get("event") == "probe_attempt":
            emit(f"(i).d 快照文件被独立进程只读打开：{'成功' if event['ok'] else '失败'} {event['ms']}ms {event.get('rows')}")

    emit("(i).e 滞后窗口 = 导出触发间隔（导出后新写入要等下一次导出才可见）：")
    for interval, label in ((300, "5 分钟一次"), (1800, "30 分钟一次"), (86400, "每天一次")):
        emit(f"        {label:12s} → 最大滞后 {interval}s；每天导出成本 ≈ {export_ms * (86400 / interval) / 1000:.1f}s CPU")


def section_three(live: Path) -> None:
    import duckdb

    emit("")
    emit("== (iii) 应用改成按需 open/close：单次开销 / 锁是否真释放 / 并发打架 ==")

    def open_close_once() -> float:
        t = time.perf_counter()
        conn = duckdb.connect(str(live))
        conn.execute("SELECT count(*) FROM batches").fetchone()
        conn.close()
        return (time.perf_counter() - t) * 1000

    hot = [open_close_once() for _ in range(20)]
    emit(f"(iii).a 单次「open + 查询 + close」：均值 {statistics.mean(hot):.1f}ms，中位 {statistics.median(hot):.1f}ms（20 次）")

    conn = duckdb.connect(str(live))
    keep = []
    for _ in range(20):
        t = time.perf_counter()
        conn.execute("SELECT count(*) FROM batches").fetchone()
        keep.append((time.perf_counter() - t) * 1000)
    conn.close()
    emit(f"(iii).b 对照：常驻连接上同一查询：均值 {statistics.mean(keep):.2f}ms（20 次）→ 每请求多付约 {statistics.mean(hot) - statistics.mean(keep):.1f}ms")

    # 锁在 close 后是否真释放
    holder = Holder("duckdb", live, mode="rw")
    holder.wait_event()
    events = []
    for i in range(3):
        probe = subprocess.run(
            [
                sys.executable,
                str(SPIKE / "probe_duckdb.py"),
                "--db",
                str(live),
                "--mode",
                "rw",
                "--sql",
                f"INSERT INTO projects VALUES ({900 + i}, 'race', 1, 'ts', NULL)",
                "--label",
                f"应用持有连接时第{i + 1}次试写",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        for line in probe.stdout.splitlines():
            event = json.loads(line)
            if event.get("event") == "probe_attempt":
                events.append(event["ok"])
    emit(f"(iii).c 应用只要还持有连接，外部进程 3 次试写结果：{events}（False = 被锁挡住）")

    # 应用松手 → 外部进程挤进来 → 应用再也拿不回来
    emit(f"(iii).d 应用 close：{json.dumps(holder.command('close'), ensure_ascii=False)}")
    outsider = Holder("duckdb", live, mode="rw")
    ready = outsider.wait_event()
    emit(f"        外部进程（模拟 MCP）抢占：pid={ready.get('pid')}")
    probe = subprocess.run(
        [sys.executable, str(SPIKE / "probe_duckdb.py"), "--db", str(live), "--mode", "rw", "--label", "应用想重新开库"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    for line in probe.stdout.splitlines():
        event = json.loads(line)
        if event.get("event") == "probe_attempt":
            emit(
                f"        应用下一个请求想开库：{'成功' if event['ok'] else '失败'}"
                + ("" if event["ok"] else f" ← {event['message'][:110]}")
            )
    outsider.stop()
    probe = subprocess.run(
        [sys.executable, str(SPIKE / "probe_duckdb.py"), "--db", str(live), "--mode", "rw", "--label", "外部进程退出后应用开库"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    for line in probe.stdout.splitlines():
        event = json.loads(line)
        if event.get("event") == "probe_attempt":
            emit(f"        外部进程退出后，应用开库：{'成功' if event['ok'] else '失败'}（{event['ms']}ms）")
    holder.stop()


def section_two() -> None:
    emit("")
    emit("== (ii) 应用侧出口（HTTP）：复用现有接口的成本与改动面 ==")

    import os
    import socket

    tmp = Path(tempfile.mkdtemp(prefix="labflow_http_"))
    os.environ["LABFLOW_DB"] = "sqlite"

    import hashlib
    import base64
    import secrets as _secrets

    def fast_hash(password, salt=None):
        salt = salt or _secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 1)
        return salt, base64.b64encode(digest).decode()

    import server.auth as auth_mod
    import server.db as db_mod
    import server.config as cfg
    import server.utils as utils_mod

    auth_mod.password_hash = fast_hash
    db_mod.password_hash = fast_hash

    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (tmp / "uploads").mkdir(exist_ok=True)
    (tmp / "static").mkdir(exist_ok=True)
    cfg.BASE_DIR, cfg.DATA_DIR = tmp, data_dir
    cfg.UPLOAD_DIR, cfg.STATIC_DIR = tmp / "uploads", tmp / "static"
    cfg.DB_PATH, cfg.SECRET_PATH = data_dir / "labflow.db", data_dir / "secret.key"
    cfg.HOST = "127.0.0.1"
    db_mod.DB_PATH = cfg.DB_PATH
    utils_mod.ensure_dirs()
    auth_mod.SECRET = auth_mod.get_secret()

    from server.db import session as db_session, init_db
    from server.handler import LabFlowHandler
    from server.models import Project, Batch
    from http.server import ThreadingHTTPServer
    import requests as req

    class QuietHandler(LabFlowHandler):
        def log_message(self, *args):  # 屏蔽每请求一行访问日志，实测日志只留结论
            return

    init_db()
    with db_session() as s:
        s.add(Project(id=1, name="project-a", created_by=1, created_at="2026-09-01"))
        for i in range(1, 201):
            s.add(
                Batch(
                    id=i,
                    project_id=1,
                    batch_no=f"B-{i}",
                    name=f"batch-{i}",
                    created_by=1,
                    created_at="2026-09-01",
                    updated_at="2026-09-01",
                )
            )

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    session_ = req.Session()
    login = session_.post(f"{base}/api/login", json={"username": "leader", "password": "labflow123"})
    emit(f"(ii).0 登录：HTTP {login.status_code}")

    def timed(url: str, n: int = 20) -> tuple[float, int]:
        samples = []
        payload = None
        for _ in range(n):
            t = time.perf_counter()
            r = session_.get(base + url)
            samples.append((time.perf_counter() - t) * 1000)
            payload = r
        if payload.status_code != 200:
            raise RuntimeError(f"{url} -> HTTP {payload.status_code}: {payload.text[:200]}")
        return statistics.mean(samples), len(payload.content)

    for url in ("/api/projects", "/api/batches?project_id=1", "/api/trash"):
        mean_ms, size = timed(url)
        emit(f"(ii).a GET {url}：均值 {mean_ms:.1f}ms，响应 {human_bytes(size)}（20 次，含本机回环）")

    server.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    emit("(ii).b 改动面：现有只读出口已覆盖 list_projects / list_batches / list_trash / list_users / file-config；")
    emit("        软删除语义在 server/serializers.py 里统一用 deleted_at IS NULL 过滤，新出口直接复用即可；")
    emit("        权限沿用 server/handler.py 的 require_user / require_manager；缺的是「任意 SQL 分析」，要新开只读查询接口。")


def main() -> int:
    import duckdb

    emit("# D5 三种绕锁形态的成本与新鲜度实测（VYB-419）")
    emit(f"环境：python {sys.version.split()[0]} / duckdb {duckdb.__version__}")
    live = build_live_db()
    emit(f"活库：{live}")
    section_one(live)
    section_three(live)
    section_two()
    emit("")
    emit("# 实测结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
