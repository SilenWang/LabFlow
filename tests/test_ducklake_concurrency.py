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
from sqlalchemy import text

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
        assert counters["projects"] == 9001
        assert db_mod.resync_id_counters()["projects"] == 9001, "重复对齐不能把计数器推回去"

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

    def test_stale_counter_does_not_reissue_ids(self, server_url):
        """计数器被拨回去时不能发出已存在的 id（DuckLake 没有主键兜底，重号会静默入库）。"""
        with db_mod.session() as s:
            s.add(Project(id=5001, name="老项目", created_by=1, created_at=now_iso()))

        conn = db_mod._counter_conn()
        try:
            conn.execute("UPDATE id_seq SET next = 3 WHERE name = 'projects'")
        finally:
            conn.close()

        with db_mod.session() as s:
            fresh = Project(name="拨回计数器之后的新项目", created_by=1, created_at=now_iso())
            s.add(fresh)
        assert fresh.id > 5001

        with db_mod.session() as s:
            ids = [row[0] for row in s.execute(text("SELECT id FROM projects ORDER BY id")).all()]
        assert len(ids) == len(set(ids)), f"出现重复 id：{ids}"


class TestNameClaims:
    """名字唯一性落在辅助 SQLite 的占位表上：改名、失败回滚、历史数据都要覆盖。"""

    def test_rename_checks_and_moves_the_claim(self, server_url, leader_session, project):
        first = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"], "batch_no": "RN-1", "name": "改名用的批次 A",
        }).json()["batch"]
        second = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"], "batch_no": "RN-2", "name": "改名用的批次 B",
        }).json()["batch"]

        # 改成别人占着的名字 → 409，且自己原来的名字还在
        r = leader_session.patch(f"{server_url}/api/batches/{second['id']}", json={"name": "改名用的批次 A"})
        assert r.status_code == 409, r.text
        with db_mod.session() as s:
            assert s.query(Batch).filter(Batch.id == second["id"]).one().name == "改名用的批次 B"

        # 改成新名字 → 200；旧名字随之空出来，能被别的批次用
        r = leader_session.patch(f"{server_url}/api/batches/{second['id']}", json={"name": "改名后的批次 B"})
        assert r.status_code == 200, r.text
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"], "batch_no": "RN-3", "name": "改名用的批次 B",
        })
        assert r.status_code == 201, r.text

        # 名字没变（原地提交同名）不能被自己的占位挡成 409
        assert leader_session.patch(
            f"{server_url}/api/batches/{first['id']}", json={"name": "改名用的批次 A"}
        ).status_code == 200

    def test_claim_is_released_when_the_write_fails(self, server_url, test_dir, leader_session, monkeypatch):
        """写失败（这里用极短重试 + 外部占锁逼出 500）要把占位放掉，名字不能永久锁死。"""
        monkeypatch.setattr(db_mod, "COMMIT_RETRY_ATTEMPTS", 1)
        holder = _start_lock_holder(test_dir, 1.0)
        try:
            r = leader_session.post(f"{server_url}/api/projects", json={"name": "失败后应释放的名字"})
        finally:
            holder.wait()
        assert r.status_code == 500, r.text

        r = leader_session.post(f"{server_url}/api/projects", json={"name": "失败后应释放的名字"})
        assert r.status_code == 201, "占位没释放，名字被永久锁死"

    def test_startup_seeds_claims_from_existing_rows(self, server_url, leader_session):
        """老库/迁移导入的行没有占位：启动对齐后，同名新增必须 409。"""
        with db_mod.session() as s:
            s.add(Project(id=6001, name="历史项目", created_by=1, created_at=now_iso()))
        # 清掉刚占的位，模拟"数据在、占位表没跟上"
        conn = db_mod._counter_conn()
        try:
            conn.execute("DELETE FROM name_claims WHERE scope = 'projects.name' AND name = '历史项目'")
        finally:
            conn.close()

        db_mod.resync_name_claims()
        r = leader_session.post(f"{server_url}/api/projects", json={"name": "历史项目"})
        assert r.status_code == 409, r.text

    def test_startup_drops_orphan_claims(self, server_url, leader_session):
        """进程崩在"占了位还没写库"之间会留下孤儿占位，启动对齐要把它清掉。"""
        conn = db_mod._counter_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO name_claims (scope, name) VALUES ('projects.name', '孤儿占位')"
            )
        finally:
            conn.close()

        db_mod.resync_name_claims()
        r = leader_session.post(f"{server_url}/api/projects", json={"name": "孤儿占位"})
        assert r.status_code == 201, r.text


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
