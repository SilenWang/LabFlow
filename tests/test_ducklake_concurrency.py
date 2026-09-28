"""D2 并发与一致性加固的用例（DL1 的三条硬要求 + 应用层 id 计数器 + 双进程读写）。

只在 LABFLOW_DB=ducklake 下有意义：sqlite / seekdb 自己会等锁、自己发号，
这些用例整体跳过。
"""

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import requests as req

import server.db as db_mod
from server.models import Batch, Project
from server.utils import now_iso

pytestmark = pytest.mark.skipif(
    db_mod.DB_BACKEND != "ducklake",
    reason="并发加固只对 DuckLake 后端有意义（sqlite/seekdb 由引擎自己排队与发号）",
)


# 外部进程占住 catalog 的 SQLite 写锁：模拟"另一个进程正在提交"，
# 也就是 DL1 实测里让 DuckLake 提交报 database is locked 的场景。
_HOLD_LOCK = """
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1])
conn.execute("BEGIN IMMEDIATE")
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
conn.commit()
"""

# 第二个进程只读 attach 活库：验证"应用在跑时另一个进程也能读"。
_READ_ONLY_COUNT = """
import duckdb, sys
catalog, data_dir, table = sys.argv[1], sys.argv[2], sys.argv[3]
conn = duckdb.connect(":memory:")
conn.execute("LOAD ducklake; LOAD sqlite")
conn.execute(f"ATTACH 'ducklake:sqlite:{catalog}' AS ro (DATA_PATH '{data_dir}', READ_ONLY)")
print(conn.execute(f"SELECT count(*) FROM ro.{table}").fetchone()[0])
"""


def _login(server_url, username, password):
    session = req.Session()
    session.headers.update({"Accept": "application/json"})
    r = session.post(f"{server_url}/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return session


def _verify_ducklake(mode, ducklake_dir, expect=None):
    repo_root = Path(__file__).resolve().parent.parent
    argv = [sys.executable, str(repo_root / "deploy" / "verify_ducklake.py"), mode, str(ducklake_dir)]
    if expect is not None:
        argv += ["--expect", str(expect)]
    return subprocess.run(argv, capture_output=True, text=True, cwd=repo_root)


def _start_lock_holder(test_dir, seconds):
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(db_mod._ducklake_catalog_path()), str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "locked", holder.stderr.read()
    return holder


class TestAttachOncePerProcess:
    def test_pool_reuses_one_attach(self, server_url):
        """池里每条连接都 ATTACH 会报 database with name "dlk" already exists（DL1 67.6%）。"""
        engine = db_mod.get_engine()
        conns, errors = [], []
        barrier = threading.Barrier(4)

        def grab():
            try:
                barrier.wait()
                conns.append(engine.raw_connection())
            except Exception as exc:  # pragma: no cover - 出问题时才走到
                errors.append(exc)

        threads = [threading.Thread(target=grab) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert errors == []
        assert len(conns) == 4
        try:
            for conn in conns:
                driver = conn.driver_connection
                rows = driver.execute(
                    "SELECT database_name FROM duckdb_databases() WHERE database_name = ?",
                    (db_mod.DUCKLAKE_ALIAS,),
                ).fetchall()
                assert rows == [(db_mod.DUCKLAKE_ALIAS,)]
                assert driver.execute("SELECT count(*) FROM projects").fetchone() is not None
        finally:
            for conn in conns:
                conn.close()

    def test_duplicate_attach_is_the_failure_we_avoid(self, server_url):
        """把被规避的报错形态钉在用例里：同别名再 ATTACH 一次就炸。"""
        conn = db_mod.get_engine().raw_connection()
        try:
            with pytest.raises(Exception, match="already exists"):
                conn.driver_connection.execute(
                    f"ATTACH 'ducklake:sqlite:{db_mod._ducklake_catalog_path()}' "
                    f"AS {db_mod.DUCKLAKE_ALIAS} (DATA_PATH '{db_mod._ducklake_dir() / 'data'}')"
                )
        finally:
            conn.close()


class TestWriteConflictRetry:
    def test_concurrent_same_row_updates_never_500(self, server_url, batch):
        """验收项：并发同改一行不再 500。"""
        results, errors = [], []
        barrier = threading.Barrier(6)
        names = [f"并发改名-{i}" for i in range(6)]

        def patch(name):
            try:
                barrier.wait()
                session = _login(server_url, "leader", "labflow123")
                results.append(
                    session.patch(f"{server_url}/api/batches/{batch['id']}", json={"name": name})
                )
            except Exception as exc:  # pragma: no cover - 出问题时才走到
                errors.append(exc)

        threads = [threading.Thread(target=patch, args=(name,)) for name in names]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert errors == []
        assert [r.status_code for r in results] == [200] * 6
        with db_mod.session() as s:
            stored = s.query(Batch).filter(Batch.id == batch["id"]).one().name
        assert stored in names, "改名必须落在库里，不能被回滚重试静默丢掉"

    def test_commit_conflict_from_other_process_is_retried(self, server_url, test_dir, batch):
        """另一个进程占着 catalog 写锁时，这一笔更新要退避重试到成功，而不是 500。"""
        holder = _start_lock_holder(test_dir, 0.8)
        try:
            started = time.monotonic()
            r = _login(server_url, "leader", "labflow123").patch(
                f"{server_url}/api/batches/{batch['id']}", json={"name": "锁冲突后改名"}
            )
            elapsed = time.monotonic() - started
        finally:
            holder.wait()
        assert r.status_code == 200, r.text
        assert elapsed >= 0.5, "锁被占住期间提交必然冲突，耗时说明重试真的发生了"
        with db_mod.session() as s:
            assert s.query(Batch).filter(Batch.id == batch["id"]).one().name == "锁冲突后改名"

    def test_insert_conflict_retries_without_duplicate(self, server_url, test_dir, leader_session):
        """新增行在冲突后重试，不能插出重复行、也不能丢写。"""
        with db_mod.session() as s:
            before = s.query(Project).count()
        holder = _start_lock_holder(test_dir, 0.8)
        try:
            r = leader_session.post(f"{server_url}/api/projects", json={"name": "冲突后新增项目"})
        finally:
            holder.wait()
        assert r.status_code == 201, r.text
        with db_mod.session() as s:
            assert s.query(Project).count() == before + 1
            assert s.query(Project).filter(Project.name == "冲突后新增项目").count() == 1

    def test_bulk_update_conflict_keeps_both_writes(self, server_url, test_dir, leader_session):
        """删除项目会同时改 ORM 对象和批量 UPDATE 批次：重试后两处都得在。"""
        project = leader_session.post(f"{server_url}/api/projects", json={"name": "待删项目"}).json()["project"]
        for i in range(2):
            r = leader_session.post(f"{server_url}/api/batches", json={
                "project_id": project["id"],
                "batch_no": f"DEL-{i}",
                "name": f"待删批次-{i}",
            })
            assert r.status_code == 201

        holder = _start_lock_holder(test_dir, 0.8)
        try:
            r = leader_session.delete(f"{server_url}/api/projects/{project['id']}")
        finally:
            holder.wait()
        assert r.status_code == 200, r.text

        with db_mod.session() as s:
            assert s.query(Project).filter(Project.id == project["id"]).one().deleted_at is not None
            batches = s.query(Batch).filter(Batch.project_id == project["id"]).all()
        assert [b.name for b in batches if b.deleted_at is None] == []


class TestSecondProcessReadWrite:
    def test_other_process_reads_live_ducklake(self, server_url, leader_session, project):
        """验收项：应用进程在跑（持有 attach）时，第二个进程仍能只读打开活库。"""
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "RO-001",
            "name": "双进程读的批次",
        })
        assert r.status_code == 201

        reader = subprocess.run(
            [
                sys.executable,
                "-c",
                _READ_ONLY_COUNT,
                str(db_mod._ducklake_catalog_path()),
                str(db_mod._ducklake_dir() / "data"),
                "batches",
            ],
            capture_output=True,
            text=True,
        )
        assert reader.returncode == 0, reader.stderr
        assert json.loads(reader.stdout) == 1

        # 读的进程退出后，应用照常写。
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "RO-002",
            "name": "读完之后再写",
        })
        assert r.status_code == 201


class TestIdCounter:
    def test_counter_resumes_from_max_id_after_import(self, server_url):
        """迁移导入历史 id 后要从 max(id)+1 续号（D0 实测：不续号下一条就撞主键）。"""
        with db_mod.session() as s:
            s.add(Project(id=9001, name="迁移导入的项目", created_by=1, created_at=now_iso()))

        counters = db_mod.resync_id_counters()
        assert counters["projects"] == 9002
        assert db_mod.resync_id_counters()["projects"] == 9002, "重复对齐不能把计数器推回去"

        with db_mod.session() as s:
            fresh = Project(name="续号的新项目", created_by=1, created_at=now_iso())
            s.add(fresh)
        assert fresh.id == 9002

    def test_startup_reseeds_stale_counter(self, server_url, leader_session):
        """计数器文件丢了也不能重号：init_db()（启动路径）要重新对齐。"""
        with db_mod.session() as s:
            s.add(Project(id=7001, name="启动前已存在的项目", created_by=1, created_at=now_iso()))
            max_id = s.query(Project).count()  # 触发一次读，确保上面那行真的落库
        assert max_id >= 1

        db_mod._ducklake_ids_path().unlink()
        db_mod.init_db()

        r = leader_session.post(f"{server_url}/api/projects", json={"name": "启动后续号项目"})
        assert r.status_code == 201, r.text
        assert r.json()["project"]["id"] > 7001

    def test_counter_is_shared_across_threads(self, server_url):
        """辅助 SQLite 计数表多线程取号不重号（DL0 的 400/400 结论在应用层复验）。"""
        ids, errors = [], []
        barrier = threading.Barrier(4)

        def take():
            try:
                barrier.wait()
                ids.extend(db_mod.next_id("projects") for _ in range(50))
            except Exception as exc:  # pragma: no cover - 出问题时才走到
                errors.append(exc)

        threads = [threading.Thread(target=take) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert errors == []
        assert len(ids) == len(set(ids)) == 200


class TestBackupCopy:
    """DuckLake 有 WAL：备份是"停服整目录拷贝"，产物要能在干净目录还原并校验行数。"""

    def test_backup_copy_restores_with_same_row_counts(self, server_url, test_dir, leader_session, project):
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "BK-001",
            "name": "备份里的批次",
        })
        assert r.status_code == 201

        # 备份产物的形态：整个 data/ducklake 目录的一份拷贝（拷的时候服务是停的）。
        source = db_mod._ducklake_dir()
        restored = test_dir / "restore" / "data" / "ducklake"
        restored.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, restored)

        expected_file = test_dir / "row-counts.json"
        backup = _verify_ducklake("counts", source)
        assert backup.returncode == 0, backup.stderr
        expected = json.loads(backup.stdout)
        assert expected["batches"] == 1
        expected_file.write_text(backup.stdout, encoding="utf-8")

        # 干净目录、路径完全变了：还能读出同样的行数，说明这份备份真的可用。
        checked = _verify_ducklake("counts", restored, expect=expected_file)
        assert checked.returncode == 0, checked.stderr
        assert json.loads(checked.stdout) == expected

    def test_probe_refuses_to_copy_a_live_library(self, server_url, test_dir):
        """备份脚本靠这条探测挡住"拷活文件"：库被占着就必须报错。"""
        live = _verify_ducklake("probe-unlocked", db_mod._ducklake_dir())
        assert live.returncode == 1, "应用进程还占着库，探测必须失败"

        copy = test_dir / "copy" / "data" / "ducklake"
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(db_mod._ducklake_dir(), copy)
        assert _verify_ducklake("probe-unlocked", copy).returncode == 0
