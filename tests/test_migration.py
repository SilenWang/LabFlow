"""sqlite → seekdb 数据迁移的验收用例。

覆盖面：行数与内容校验和一致、自增主键保留、重复执行拒绝二次导入、
dry-run 不落盘，以及迁移后两套后端的 API 响应完全相同。
"""

import json
import socket
import sqlite3
import threading
from http.server import ThreadingHTTPServer

import pymysql
import pytest
import requests
from sqlalchemy import create_engine

import server.config as cfg
import server.db as db_mod
from server import migrate_sqlite_to_seekdb as mig
from server.handler import LabFlowHandler
from server.models import Base


def _seekdb_usable(db_dir):
    """本机能否起 seekdb；不能则跳过用例，避免在无 seekdb 的环境里误报。"""
    try:
        db_mod.open_seekdb(db_dir=db_dir)
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        return False, repr(exc)
    finally:
        db_mod.close_seekdb()
    return True, ""


@pytest.fixture(scope="module")
def seekdb_available(tmp_path_factory):
    ok, reason = _seekdb_usable(tmp_path_factory.mktemp("seekdb_probe"))
    if not ok:
        pytest.skip(f"seekdb 不可用，跳过迁移用例: {reason}")
    return True


def _sqlite_source(db_path):
    """按 models 建源库并灌入 4 张表的样例数据，故意混入 NULL/空串/中文/特殊字符。"""
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO users (id, username, display_name, role, password_salt,"
        " password_hash, active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "leader", "总负责人", "manager", "s1", "h1", 1, "2026-01-01T00:00:00+00:00"),
            (2, "chem1", "化学 1", "chem", "s2", "h2", 1, "2026-01-02T00:00:00+00:00"),
            (5, "bio1", "生物 1 50% ' 引号", "bio", "s3", "h3", 0, "2026-01-03T00:00:00+00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO projects (id, name, created_by, created_at, deleted_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (1, "项目甲", 1, "2026-01-01T00:00:00+00:00", None),
            (7, "项目乙", 2, "2026-01-02T00:00:00+00:00", "2026-02-01T00:00:00+00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO batches (id, project_id, batch_no, name, remark,"
        " synthesis_submitted_date, synthesis_completed_date, bio_test_start_date,"
        " bio_test_completed_date, created_by, created_at, updated_at, deleted_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 1, "BATCH-001", "批次-001", "备注", "2026-01-05", "2026-01-06",
             None, "", 1, "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", None),
            (9, 1, "BATCH-002", "批次-002", "", None, None, "2026-01-07", None,
             2, "2026-01-03T00:00:00+00:00", "2026-01-03T00:00:00+00:00",
             "2026-03-01T00:00:00+00:00"),
            (12, 7, "BATCH-003", "批次-003", None, None, None, None, None,
             1, "2026-01-04T00:00:00+00:00", "2026-01-04T00:00:00+00:00", None),
        ],
    )
    conn.executemany(
        "INSERT INTO file_versions (id, batch_id, file_type, original_name,"
        " storage_path, size_bytes, uploaded_by, uploaded_at, deleted_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (3, 1, "compound_info", "化合物信息.xlsx", "uploads/a/b.xlsx", 0, 1,
             "2026-01-05T01:00:00+00:00", None),
            (4, 1, "compound_info", "化合物信息-v2.xlsx", "uploads/a/c.xlsx",
             2147483647, 2, "2026-01-05T02:00:00+00:00", None),
            (8, 9, "bio_raw_data", "data.csv", "uploads/d/e.csv", 12345, 5,
             "2026-01-06T01:00:00+00:00", "2026-04-01T00:00:00+00:00"),
        ],
    )
    conn.commit()
    conn.close()


def _open_target(seekdb_dir):
    _, opts = db_mod.open_seekdb(db_dir=seekdb_dir)
    conn = pymysql.connect(
        **opts, database=db_mod.SEEKDB_DATABASE, charset="utf8mb4", autocommit=True
    )
    return conn


def _close_target(conn):
    conn.close()
    db_mod.close_seekdb()


def _run(sqlite_path, seekdb_dir, *extra):
    return mig.main(
        ["--sqlite", str(sqlite_path), "--seekdb-dir", str(seekdb_dir), *extra]
    )


class TestMigrateContent:
    def test_rows_and_checksums_match(self, tmp_path, seekdb_available):
        src = tmp_path / "labflow.db"
        _sqlite_source(src)
        target = tmp_path / "seekdb"

        assert _run(src, target) == mig.EXIT_OK

        conn = _open_target(target)
        try:
            for table in mig.TABLE_ORDER:
                columns, source_rows = mig.read_sqlite_table(src, table)
                target_rows = mig.target_rows(conn, table, columns)
                assert len(target_rows) == len(source_rows), table
                assert mig.rows_checksum(columns, source_rows) == mig.rows_checksum(
                    columns, target_rows
                ), table
        finally:
            _close_target(conn)

    def test_auto_increment_continues_after_ids(self, tmp_path, seekdb_available):
        src = tmp_path / "labflow.db"
        _sqlite_source(src)
        target = tmp_path / "seekdb"
        assert _run(src, target) == mig.EXIT_OK

        conn = _open_target(target)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (username, display_name, role, password_salt,"
                    " password_hash, active, created_at)"
                    " VALUES ('new', '新人', 'chem', 's', 'h', 1, '2026-05-01T00:00:00+00:00')"
                )
                new_id = cur.lastrowid
                cur.execute("SELECT MAX(id) FROM users")
                assert new_id == cur.fetchone()[0]
            # 迁移过来的 id 原样保留
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users ORDER BY id")
                assert [r[0] for r in cur.fetchall()] == [1, 2, 5, new_id]
        finally:
            _close_target(conn)

    def test_dry_run_writes_nothing(self, tmp_path, seekdb_available):
        src = tmp_path / "labflow.db"
        _sqlite_source(src)
        target = tmp_path / "seekdb"

        assert _run(src, target, "--dry-run") == mig.EXIT_OK
        assert not target.exists()


class TestMigrateRerun:
    def test_second_run_is_refused_and_target_untouched(self, tmp_path, seekdb_available):
        src = tmp_path / "labflow.db"
        _sqlite_source(src)
        target = tmp_path / "seekdb"

        assert _run(src, target) == mig.EXIT_OK
        conn = _open_target(target)
        try:
            before = {t: mig.target_count(conn, t) for t in mig.TABLE_ORDER}
        finally:
            _close_target(conn)

        assert _run(src, target) == mig.EXIT_TARGET_NOT_EMPTY

        conn = _open_target(target)
        try:
            after = {t: mig.target_count(conn, t) for t in mig.TABLE_ORDER}
        finally:
            _close_target(conn)
        assert after == before

    def test_missing_source_is_a_clean_failure(self, tmp_path, seekdb_available):
        target = tmp_path / "seekdb"
        assert _run(tmp_path / "nope.db", target) == mig.EXIT_USAGE


def _col(nullable=False, default=None, extra=""):
    """``SHOW COLUMNS`` 里一列的元信息，只取预检用得到的字段。"""
    return {"nullable": nullable, "default": default, "extra": extra}


class TestColumnPreflight:
    """列集合预检是纯函数，不依赖 seekdb。"""

    def test_source_extra_column_is_rejected(self):
        # 源库比模型多一列 → INSERT 会撞 1054，预检必须先拦下。
        target = {"id": _col(extra="auto_increment"), "username": _col()}
        problems = mig.column_problems(["id", "username", "legacy_note"], target)
        assert len(problems) == 1
        assert "legacy_note" in problems[0]

    def test_target_extra_nullable_column_is_allowed(self):
        # 旧 sqlite 库还没补上 batches.remark：目标多出的可空列不该挡住迁移。
        target = {"id": _col(extra="auto_increment"), "remark": _col(nullable=True)}
        assert mig.column_problems(["id"], target) == []

    def test_target_extra_column_with_default_is_allowed(self):
        target = {"id": _col(extra="auto_increment"), "active": _col(default="1")}
        assert mig.column_problems(["id"], target) == []

    def test_target_extra_required_column_is_rejected(self):
        # 目标新增必填列而源库无数据 → 1364，同样是写不进去。
        target = {"id": _col(extra="auto_increment"), "must": _col()}
        problems = mig.column_problems(["id"], target)
        assert len(problems) == 1
        assert "must" in problems[0]

    def test_matching_columns_have_no_problems(self):
        target = {"id": _col(extra="auto_increment"), "name": _col()}
        assert mig.column_problems(["id", "name"], target) == []


class TestMigrateSchemaDrift:
    """源库存在模型里已经没有的历史列（结构漂移）时的收口行为。"""

    def test_extra_source_column_exits_2_without_writing(
        self, tmp_path, seekdb_available, capsys
    ):
        src = tmp_path / "labflow.db"
        _sqlite_source(src)
        conn = sqlite3.connect(src)
        conn.execute("ALTER TABLE users ADD COLUMN legacy_note TEXT")
        conn.execute("UPDATE users SET legacy_note = '历史遗留备注'")
        conn.commit()
        conn.close()

        target = tmp_path / "seekdb"
        assert _run(src, target) == mig.EXIT_USAGE

        err = capsys.readouterr().err
        assert "legacy_note" in err
        assert "Traceback" not in err

        # 没有半成品：4 张表都是空的，重跑不会被「目标库非空」挡下。
        conn = _open_target(target)
        try:
            assert {t: mig.target_count(conn, t) for t in mig.TABLE_ORDER} == {
                t: 0 for t in mig.TABLE_ORDER
            }
        finally:
            _close_target(conn)

        # 源库对齐后直接重跑即可，不需要人工清库。
        conn = sqlite3.connect(src)
        conn.execute("ALTER TABLE users DROP COLUMN legacy_note")
        conn.commit()
        conn.close()
        assert _run(src, target) == mig.EXIT_OK

        conn = _open_target(target)
        try:
            assert mig.target_count(conn, "users") == 3
        finally:
            _close_target(conn)

    def test_source_missing_optional_column_still_migrates(
        self, tmp_path, seekdb_available
    ):
        # 反向漂移：旧 sqlite 库少了可空的 batches.remark，仍应正常搬运。
        src = tmp_path / "labflow.db"
        _sqlite_source(src)
        conn = sqlite3.connect(src)
        conn.execute("ALTER TABLE batches DROP COLUMN remark")
        conn.commit()
        conn.close()

        target = tmp_path / "seekdb"
        assert _run(src, target) == mig.EXIT_OK

        conn = _open_target(target)
        try:
            columns, rows = mig.read_sqlite_table(src, "batches")
            assert len(mig.target_rows(conn, "batches", columns)) == len(rows)
        finally:
            _close_target(conn)


class TestMigrateWriteFailure:
    """写入中途失败（不是结构漂移）时同样不能留半成品。"""

    def test_write_failure_leaves_target_empty_and_rerunnable(
        self, tmp_path, seekdb_available, capsys
    ):
        src = tmp_path / "labflow.db"
        _sqlite_source(src)
        # batches 是第 3 张表：users / projects 已经写进去之后才会失败。
        conn = sqlite3.connect(src)
        conn.execute("UPDATE batches SET name = ? WHERE id = 1", ("超长" * 100,))
        conn.commit()
        conn.close()

        target = tmp_path / "seekdb"
        assert _run(src, target) == mig.EXIT_USAGE

        err = capsys.readouterr().err
        assert "Traceback" not in err

        conn = _open_target(target)
        try:
            assert {t: mig.target_count(conn, t) for t in mig.TABLE_ORDER} == {
                t: 0 for t in mig.TABLE_ORDER
            }
        finally:
            _close_target(conn)

        # 数据修好后直接重跑，不需要人工清库。
        conn = sqlite3.connect(src)
        conn.execute("UPDATE batches SET name = ? WHERE id = 1", ("批次-001",))
        conn.commit()
        conn.close()
        assert _run(src, target) == mig.EXIT_OK

    def test_foreign_key_failure_also_cleans_up(self, tmp_path, seekdb_available):
        """非 DataError 的写入失败（外键悬空）走兜底分支，同样不留半成品。"""
        src = tmp_path / "labflow.db"
        _sqlite_source(src)
        # sqlite 默认不开外键，悬空引用能存进源库，搬到 seekdb 时才会被拒。
        conn = sqlite3.connect(src)
        conn.execute("UPDATE batches SET project_id = 999 WHERE id = 1")
        conn.commit()
        conn.close()

        target = tmp_path / "seekdb"
        assert _run(src, target) == mig.EXIT_USAGE

        conn = _open_target(target)
        try:
            assert {t: mig.target_count(conn, t) for t in mig.TABLE_ORDER} == {
                t: 0 for t in mig.TABLE_ORDER
            }
        finally:
            _close_target(conn)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server():
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), LabFlowHandler)
    # 非守护线程：server_close() 会等处理线程收尾，避免客户端已拿到响应、
    # handler 却还在关 session 的时候把后端换掉。
    server.daemon_threads = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def _ok(response, code):
    assert response.status_code == code, f"{response.request.url}: {response.text}"
    return response


def _login(base_url, username, password):
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    r = session.post(
        f"{base_url}/api/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return session


def _upload(base_url, session, batch_id, file_type, filename, content):
    return _ok(session.post(
        f"{base_url}/api/batches/{batch_id}/files",
        files={"file_type": (None, file_type), "file": (filename, content)},
    ), 201)


VIEWS = ("/api/me", "/api/users", "/api/projects", "/api/batches", "/api/trash", "/api/file-config")


def _snapshot(base_url):
    """登录三种角色，抓一遍只读接口 + 文件下载，返回可比对的结构。"""
    snap = {}
    for username, password in (
        ("leader", "labflow123"),
        ("chem1", "chem123"),
        ("bio1", "bio123"),
    ):
        session = _login(base_url, username, password)
        for path in VIEWS:
            r = session.get(f"{base_url}{path}")
            snap[f"{username} {path}"] = [r.status_code, r.json()]
        for batch in snap[f"{username} /api/batches"][1]["batches"]:
            for versions in batch["files"].values():
                for item in versions["versions"]:
                    r = session.get(f"{base_url}/api/files/{item['id']}/download")
                    snap[f"{username} file {item['id']}"] = [
                        r.status_code,
                        r.content.hex(),
                    ]
    return snap


def _seed_session(base_url):
    """通过 API 造一份带历史版本、软删除和字段编辑的数据。"""
    leader = _login(base_url, "leader", "labflow123")
    chem = _login(base_url, "chem1", "chem123")
    bio = _login(base_url, "bio1", "bio123")

    p1 = _ok(leader.post(f"{base_url}/api/projects", json={"name": "项目甲"}), 201).json()["project"]
    p2 = _ok(leader.post(f"{base_url}/api/projects", json={"name": "项目乙"}), 201).json()["project"]

    b1 = _ok(leader.post(f"{base_url}/api/batches", json={
        "project_id": p1["id"], "batch_no": "BATCH-001", "name": "批次-001",
        "remark": "备注 50% ' 引号",
    }), 201).json()["batch"]
    b2 = _ok(chem.post(f"{base_url}/api/batches", json={
        "project_id": p1["id"], "batch_no": "BATCH-002", "name": "批次-002",
    }), 201).json()["batch"]
    b3 = _ok(leader.post(f"{base_url}/api/batches", json={
        "project_id": p2["id"], "batch_no": "BATCH-003", "name": "批次-003",
    }), 201).json()["batch"]

    _ok(leader.patch(f"{base_url}/api/batches/{b1['id']}", json={
        "synthesis_submitted_date": "2026-01-05",
        "synthesis_completed_date": "2026-01-06",
    }), 200)
    _ok(chem.patch(f"{base_url}/api/batches/{b1['id']}", json={"batch_no": "BATCH-001A"}), 200)
    _ok(bio.patch(f"{base_url}/api/batches/{b1['id']}", json={
        "bio_test_start_date": "2026-01-07",
    }), 200)

    _upload(base_url, leader, b1["id"], "compound_info", "化合物信息.xlsx", b"v1")
    _upload(base_url, chem, b1["id"], "compound_info", "化合物信息-v2.xlsx", b"v2-data")
    _upload(base_url, bio, b1["id"], "bio_raw_data", "data.csv", b"a,b\n1,2\n")
    _upload(base_url, leader, b2["id"], "data_summary", "汇总.docx", b"summary")

    # 软删除：一个批次（进回收站）+ 一个历史文件
    _ok(leader.delete(f"{base_url}/api/batches/{b3['id']}"), 200)
    for batch in leader.get(f"{base_url}/api/batches").json()["batches"]:
        if batch["id"] == b1["id"]:
            victim = batch["files"]["compound_info"]["versions"][-1]["id"]
            _ok(leader.delete(f"{base_url}/api/files/{victim}"), 200)
            break
    else:
        raise AssertionError("批次-001 不在列表里，测试数据没造对")


class TestApiParityAfterMigration:
    def test_api_responses_are_identical(self, server_url, monkeypatch, seekdb_available):
        if db_mod.DB_BACKEND != "sqlite":
            pytest.skip("整轮用例跑在 seekdb 后端下，没有 sqlite 源库可比对")
        _seed_session(server_url)
        sqlite_snapshot = _snapshot(server_url)

        # 把现网 sqlite 库搬进 seekdb（照运行时用的目标目录）
        seekdb_dir = cfg.DATA_DIR / "seekdb"
        assert _run(cfg.DB_PATH, seekdb_dir) == mig.EXIT_OK

        # 切到 seekdb 后端，起第二个实例
        monkeypatch.setattr(db_mod, "DB_BACKEND", "seekdb")
        db_mod.init_db()
        server, base_url = _start_server()
        try:
            seekdb_snapshot = _snapshot(base_url)
        finally:
            server.shutdown()
            server.server_close()
            # /api/trash 曾因 serialize_deleted_batch 跑在 with db_session() 之外
            # 泄漏连接（要等 GC 才还池）；已修复，这里不再需要 gc.collect()，
            # 直接断言池里没有借出未还的连接，顺带守住这个回归。
            assert db_mod._engine.pool.checkedout() == 0
            # 切回 sqlite 收尾：init_db() 会先 dispose seekdb 引擎再关实例。
            monkeypatch.setattr(db_mod, "DB_BACKEND", "sqlite")
            db_mod.init_db()

        assert set(sqlite_snapshot) == set(seekdb_snapshot)
        diff = {
            key: [sqlite_snapshot[key], seekdb_snapshot[key]]
            for key in sqlite_snapshot
            if sqlite_snapshot[key] != seekdb_snapshot[key]
        }
        assert not diff, json.dumps(diff, ensure_ascii=False, indent=2)
