"""独立验收探针：并发同名新增（D1 验收带出的场景）。

D2 复核用例落库：唯一性必须由带 UNIQUE 的辅助 SQLite 占位表原子兜住，
并发同名只能 1×201 + N×409，且库里同名只有一行。
"""

import subprocess
import sys
import threading
import time

import pytest
import requests as req

import server.db as db_mod
from server.models import Batch, Project

pytestmark = pytest.mark.skipif(
    db_mod.DB_BACKEND != "ducklake", reason="只验 ducklake 后端"
)

# 另一个进程占住 catalog 的写锁，逼应用真的走重试路径（同 _HOLD_LOCK）。
_HOLD_LOCK = """
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1])
conn.execute("BEGIN IMMEDIATE")
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
conn.commit()
"""


def _start_lock_holder(seconds):
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(db_mod._ducklake_catalog_path()), str(seconds)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert holder.stdout.readline().strip() == "locked", holder.stderr.read()
    return holder


def _login(server_url):
    s = req.Session()
    s.headers.update({"Accept": "application/json"})
    r = s.post(f"{server_url}/api/login", json={"username": "leader", "password": "labflow123"})
    assert r.status_code == 200, r.text
    return s


def _run_concurrent(fn, n):
    results, errors = [], []
    barrier = threading.Barrier(n)

    def worker(i):
        try:
            barrier.wait()
            results.append(fn(i))
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors
    return results


class TestSameNameConcurrency:
    def test_eight_concurrent_same_name_projects(self, server_url):
        def create(i):
            s = _login(server_url)
            return s.post(f"{server_url}/api/projects", json={"name": "同名并发项目"})

        results = _run_concurrent(create, 8)
        codes = sorted(r.status_code for r in results)
        with db_mod.session() as s:
            rows = s.query(Project).filter(Project.name == "同名并发项目").count()
        print("\n[probe] project codes:", codes, "rows:", rows)
        for r in results:
            if r.status_code not in (201, 409):
                print("[probe] body:", r.status_code, r.text)
        assert rows == 1, f"库里同名项目 {rows} 条（期望 1）：静默重号"
        assert codes == [201] + [409] * 7, f"codes={codes}"

    def test_twelve_rounds_same_name_batches(self, server_url, project):
        bad = []
        for rnd in range(12):
            name = f"同名批次-{rnd}"

            def create(i, rnd=rnd, name=name):
                s = _login(server_url)
                return s.post(f"{server_url}/api/batches", json={
                    "project_id": project["id"], "batch_no": f"R{rnd}-{i}", "name": name,
                })

            results = _run_concurrent(create, 2)
            codes = sorted(r.status_code for r in results)
            with db_mod.session() as s:
                rows = s.query(Batch).filter(Batch.name == name).count()
            print(f"[probe] round {rnd}: codes={codes} rows={rows}")
            if codes != [201, 409] or rows != 1:
                bad.append((rnd, codes, rows))
                for r in results:
                    print("[probe] body:", r.status_code, r.text[:300])
        assert bad == [], f"异常轮次(round, codes, rows)={bad}"

    def test_retry_path_itself_can_duplicate(self, server_url):
        """外部进程占锁逼出真冲突重试：两个同名请求都走重试，看会不会插出两条。"""
        holder = _start_lock_holder(1.5)
        started = time.monotonic()
        try:
            results = _run_concurrent(
                lambda i: _login(server_url).post(
                    f"{server_url}/api/projects", json={"name": "重试路径同名项目"}
                ),
                2,
            )
        finally:
            holder.wait()
        elapsed = time.monotonic() - started
        codes = sorted(r.status_code for r in results)
        with db_mod.session() as s:
            rows = s.query(Project).filter(Project.name == "重试路径同名项目").count()
        print(f"\n[probe] retry-path codes={codes} rows={rows} elapsed={elapsed:.2f}s")
        assert elapsed > 0.5, "没等到锁释放，说明没走重试"
        assert rows == 1, f"重试路径插出 {rows} 条同名项目（期望 1）"
        assert codes == [201, 409], f"codes={codes}"
