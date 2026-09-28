"""sqlite → DuckLake 数据迁移的验收用例（VYB-417 / D3）。

覆盖面：

- 4 表（users / projects / batches / file_versions）行数与内容**独立核对**：直接用
  stdlib ``sqlite3`` 与裸 ``duckdb`` 各读一遍逐行比对，不复用脚本自己的
  ``rows_checksum``/``verify_table``。
- id 原样保留，且迁移后第一条新插入续在 ``max(id)`` 之后（id 计数器 restart）。
- 退出码语义：0 成功 / 1 校验不一致 / 2 预检失败 / 3 目标非空。
- 结构漂移、写入中途失败都不留半成品，重跑不被「目标库非空」挡住。
- 迁移后**全部 API（读 + 写）**响应与现网 sqlite 一致。

迁移脚本本身与 ``LABFLOW_DB`` 无关（自建 DuckLake 引擎），所以内容类用例在两套后端
下都跑；只有「与现网 sqlite 逐接口比对」这条要求进程是从 sqlite 起跑的。
"""

import json
import socket
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import duckdb
import pytest
import requests
from sqlalchemy import create_engine

import server.config as cfg
import server.db as db_mod
import server.handler as handler_mod
from server import migrate_sqlite_to_ducklake as mig
from server.handler import LabFlowHandler
from server.models import Base


# --------------------------------------------------------------------------- #
# 独立核对用的读库助手：不碰迁移脚本里的任何比对函数
# --------------------------------------------------------------------------- #
def _sqlite_rows(sqlite_path, table):
    """直接用 stdlib 读源库，返回 (列名, 按 id 排序的行)。"""
    conn = sqlite3.connect(f"file:{Path(sqlite_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]
        ids = ", ".join(f'"{c}"' for c in columns)
        rows = conn.execute(f'SELECT {ids} FROM "{table}" ORDER BY id').fetchall()
    finally:
        conn.close()
    return columns, rows


def _ducklake_rows(ducklake_dir, table, columns):
    """用裸 duckdb 另开一条连接读目标，不经过 SQLAlchemy / 迁移脚本。"""
    ducklake_dir = Path(ducklake_dir)
    con = duckdb.connect(str(ducklake_dir / "client.duckdb"))
    try:
        con.execute("LOAD ducklake; LOAD sqlite")
        con.execute(
            f"ATTACH IF NOT EXISTS 'ducklake:sqlite:{ducklake_dir / 'catalog.sqlite'}' "
            f"AS dlk (DATA_PATH '{ducklake_dir / 'data'}')"
        )
        con.execute("SET search_path='dlk'")
        ids = ", ".join(f'"{c}"' for c in columns)
        return con.execute(f"SELECT {ids} FROM dlk.main.{table} ORDER BY id").fetchall()
    finally:
        con.close()


def _assert_tables_identical(sqlite_path, ducklake_dir):
    """逐表独立比对：列集合 + 逐行内容必须一模一样。"""
    for table in mig.TABLE_ORDER:
        columns, source_rows = _sqlite_rows(sqlite_path, table)
        target_rows = _ducklake_rows(ducklake_dir, table, columns)
        assert target_rows == source_rows, table
    return True


# --------------------------------------------------------------------------- #
# 源库样例数据：混入 NULL / 空串 / 中文 / 特殊字符 / 跳号 id
# --------------------------------------------------------------------------- #
def _sqlite_source(db_path):
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
    return db_path


def _run(sqlite_path, ducklake_dir, *extra):
    return mig.main(
        ["--sqlite", str(sqlite_path), "--ducklake-dir", str(ducklake_dir), *extra]
    )


class TestMigrateContent:
    def test_rows_and_content_are_identical(self, tmp_path):
        src = _sqlite_source(tmp_path / "labflow.db")
        target = tmp_path / "ducklake"

        assert _run(src, target) == mig.EXIT_OK

        # 独立核对：不是复用脚本的 rows_checksum / verify_table。
        assert _assert_tables_identical(src, target)

    def test_ids_are_preserved_and_counter_continues(self, tmp_path):
        src = _sqlite_source(tmp_path / "labflow.db")
        target = tmp_path / "ducklake"
        assert _run(src, target) == mig.EXIT_OK

        for table, expected_max in (
            ("users", 5),
            ("projects", 7),
            ("batches", 12),
            ("file_versions", 8),
        ):
            columns, rows = _sqlite_rows(src, table)
            assert [r[0] for r in rows] == sorted(r[0] for r in rows)
            assert max(r[0] for r in rows) == expected_max
            # 迁移后第一条新插入必须续在历史 max(id) 之后，不能撞上搬过来的主键。
            assert db_mod.next_id(table, ducklake_dir=target) == expected_max + 1

    def test_empty_table_counter_starts_at_one(self, tmp_path):
        src = tmp_path / "labflow.db"
        engine = create_engine(f"sqlite:///{src}")
        Base.metadata.create_all(engine)
        engine.dispose()

        target = tmp_path / "ducklake"
        assert _run(src, target) == mig.EXIT_OK
        assert db_mod.next_id("users", ducklake_dir=target) == 1

    def test_dry_run_writes_nothing(self, tmp_path):
        src = _sqlite_source(tmp_path / "labflow.db")
        target = tmp_path / "ducklake"

        assert _run(src, target, "--dry-run") == mig.EXIT_OK
        assert not target.exists()


class TestMigrateRerun:
    def test_second_run_is_refused_and_target_untouched(self, tmp_path):
        src = _sqlite_source(tmp_path / "labflow.db")
        target = tmp_path / "ducklake"

        assert _run(src, target) == mig.EXIT_OK
        before = _ducklake_snapshot(target)

        assert _run(src, target) == mig.EXIT_TARGET_NOT_EMPTY
        assert _ducklake_snapshot(target) == before

    def test_missing_source_is_a_clean_failure(self, tmp_path):
        assert _run(tmp_path / "nope.db", tmp_path / "ducklake") == mig.EXIT_USAGE


def _ducklake_snapshot(ducklake_dir):
    """目标库全量快照（行数与逐行内容），用来断言「未做任何写入」。"""
    snap = {}
    for table in mig.TABLE_ORDER:
        con = duckdb.connect(str(Path(ducklake_dir) / "client.duckdb"))
        try:
            con.execute("LOAD ducklake; LOAD sqlite")
            con.execute(
                f"ATTACH IF NOT EXISTS 'ducklake:sqlite:{Path(ducklake_dir) / 'catalog.sqlite'}' "
                f"AS dlk (DATA_PATH '{Path(ducklake_dir) / 'data'}')"
            )
            con.execute("SET search_path='dlk'")
            cols = [r[0] for r in con.execute(f"DESCRIBE dlk.main.{table}").fetchall()]
            ids = ", ".join(f'"{c}"' for c in cols)
            snap[table] = con.execute(
                f"SELECT {ids} FROM dlk.main.{table} ORDER BY id"
            ).fetchall()
        finally:
            con.close()
    return snap


def _col(nullable=False, default=None):
    """``DESCRIBE`` 里一列的元信息，只取预检用得到的字段。"""
    return {"nullable": nullable, "default": default}


class TestColumnPreflight:
    """列集合预检是纯函数，不依赖 DuckLake。"""

    def test_source_extra_column_is_rejected(self):
        target = {"id": _col(), "username": _col()}
        problems = mig.column_problems(["id", "username", "legacy_note"], target)
        assert len(problems) == 1
        assert "legacy_note" in problems[0]

    def test_target_extra_nullable_column_is_allowed(self):
        target = {"id": _col(), "remark": _col(nullable=True)}
        assert mig.column_problems(["id"], target) == []

    def test_target_extra_column_with_default_is_allowed(self):
        target = {"id": _col(), "active": _col(default="1")}
        assert mig.column_problems(["id"], target) == []

    def test_target_extra_required_column_is_rejected(self):
        target = {"id": _col(), "must": _col()}
        problems = mig.column_problems(["id"], target)
        assert len(problems) == 1
        assert "must" in problems[0]

    def test_matching_columns_have_no_problems(self):
        target = {"id": _col(), "name": _col()}
        assert mig.column_problems(["id", "name"], target) == []


class TestSchemaDrift:
    """源库存在模型里已经没有的历史列（结构漂移）时的收口行为。"""

    def test_extra_source_column_exits_2_without_writing(self, tmp_path, capsys):
        src = _sqlite_source(tmp_path / "labflow.db")
        conn = sqlite3.connect(src)
        conn.execute("ALTER TABLE users ADD COLUMN legacy_note TEXT")
        conn.execute("UPDATE users SET legacy_note = '历史遗留备注'")
        conn.commit()
        conn.close()

        target = tmp_path / "ducklake"
        assert _run(src, target) == mig.EXIT_USAGE

        err = capsys.readouterr().err
        assert "legacy_note" in err
        assert "Traceback" not in err

        # 没有半成品：4 张表都是空的，重跑不会被「目标库非空」挡下。
        engine = db_mod.make_ducklake_engine(target)
        try:
            assert {t: mig.target_count(engine, t) for t in mig.TABLE_ORDER} == {
                t: 0 for t in mig.TABLE_ORDER
            }
        finally:
            engine.dispose()

        # 源库对齐后直接重跑即可，不需要人工清库。
        conn = sqlite3.connect(src)
        conn.execute("ALTER TABLE users DROP COLUMN legacy_note")
        conn.commit()
        conn.close()
        assert _run(src, target) == mig.EXIT_OK
        assert _assert_tables_identical(src, target)

    def test_source_missing_optional_column_still_migrates(self, tmp_path):
        # 反向漂移：旧 sqlite 库少了可空的 batches.remark，仍应正常搬运。
        src = _sqlite_source(tmp_path / "labflow.db")
        conn = sqlite3.connect(src)
        conn.execute("ALTER TABLE batches DROP COLUMN remark")
        conn.commit()
        conn.close()

        target = tmp_path / "ducklake"
        assert _run(src, target) == mig.EXIT_OK
        assert _assert_tables_identical(src, target)


class TestWriteFailure:
    """写入中途失败（不是结构漂移）时同样不能留半成品。"""

    def test_not_null_violation_leaves_target_empty_and_rerunnable(self, tmp_path, capsys):
        src = tmp_path / "labflow.db"
        # 手搓一张 display_name 可为 NULL 的 users：让第二张表之后才炸。
        conn = sqlite3.connect(src)
        conn.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL,
              display_name TEXT, role TEXT NOT NULL, password_salt TEXT NOT NULL,
              password_hash TEXT NOT NULL, active INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
              created_by INTEGER NOT NULL, created_at TEXT NOT NULL, deleted_at TEXT);
            CREATE TABLE batches (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
              batch_no TEXT NOT NULL, name TEXT NOT NULL, remark TEXT,
              synthesis_submitted_date TEXT, synthesis_completed_date TEXT,
              bio_test_start_date TEXT, bio_test_completed_date TEXT,
              created_by INTEGER NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, deleted_at TEXT);
            CREATE TABLE file_versions (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL,
              file_type TEXT NOT NULL, original_name TEXT NOT NULL, storage_path TEXT NOT NULL,
              size_bytes INTEGER NOT NULL, uploaded_by INTEGER NOT NULL,
              uploaded_at TEXT NOT NULL, deleted_at TEXT);
            INSERT INTO users VALUES (1, 'leader', NULL, 'manager', 's', 'h', 1, 't');
            """
        )
        conn.commit()
        conn.close()

        target = tmp_path / "ducklake"
        assert _run(src, target) == mig.EXIT_USAGE
        assert "Traceback" not in capsys.readouterr().err

        engine = db_mod.make_ducklake_engine(target)
        try:
            assert {t: mig.target_count(engine, t) for t in mig.TABLE_ORDER} == {
                t: 0 for t in mig.TABLE_ORDER
            }
            # 计数器也被归零：重跑后新记录从 1 开始，不会跳过号段。
            assert db_mod.next_id("users", ducklake_dir=target) == 1
        finally:
            engine.dispose()

        # 数据修好后直接重跑，不需要人工清库。
        conn = sqlite3.connect(src)
        conn.execute("UPDATE users SET display_name = '总负责人'")
        conn.commit()
        conn.close()
        assert _run(src, target) == mig.EXIT_OK
        assert _assert_tables_identical(src, target)


# --------------------------------------------------------------------------- #
# 迁移后 API 与现网 sqlite 逐接口比对
# --------------------------------------------------------------------------- #
_TIMESTAMP_KEYS = {
    "created_at", "updated_at", "deleted_at", "uploaded_at",
    "project_deleted_at", "batch_deleted_at",
}


def _normalize(value):
    """把时间戳字段收敛成占位符；其余原样，id 不在归一化之列（必须逐字相等）。"""
    if isinstance(value, dict):
        return {
            key: "<ts>" if key in _TIMESTAMP_KEYS else _normalize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


class _Recorder:
    """按顺序记录每一次接口调用的状态码与（归一化后的）响应体。

    记录的是去掉 base_url 的相对路径：两次跑分别在不同的临时端口上，比对的必须是
    接口语义而不是端口号。
    """

    def __init__(self, base_url=""):
        self.base_url = base_url
        self.entries = []

    def call(self, label, session, method, url, **kwargs):
        response = session.request(method, url, **kwargs)
        try:
            body = _normalize(response.json())
        except ValueError:
            body = response.content.hex()
        path = url[len(self.base_url):] if self.base_url and url.startswith(self.base_url) else url
        self.entries.append(
            {"op": label, "method": method, "path": path, "status": response.status_code, "body": body}
        )
        return response


def _login(base_url, username, password):
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    response = session.post(
        f"{base_url}/api/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return session


def _exercise_writes(base_url, recorder):
    """在同一个（已迁移到位的）库上跑一遍写接口，覆盖 4 张表的增删改与权限/校验分支。"""
    def session_for(username, password):
        session = requests.Session()
        session.headers.update({"Accept": "application/json"})
        r = recorder.call(f"login {username}", session, "POST", f"{base_url}/api/login",
                          json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return session

    leader = session_for("leader", "labflow123")
    chem = session_for("chem1", "chem123")
    bio = session_for("bio1", "bio123")

    r = recorder.call("create project", leader, "POST", f"{base_url}/api/projects",
                      json={"name": "写-项目甲"})
    pid = r.json()["project"]["id"]
    recorder.call("duplicate project name", leader, "POST", f"{base_url}/api/projects",
                  json={"name": "写-项目甲"})
    recorder.call("empty project name", leader, "POST", f"{base_url}/api/projects",
                  json={"name": "   "})
    recorder.call("create project forbidden", chem, "POST", f"{base_url}/api/projects",
                  json={"name": "写-项目乙"})
    recorder.call("rename project", leader, "PATCH", f"{base_url}/api/projects/{pid}",
                  json={"name": "写-项目甲-改"})
    recorder.call("rename missing project", leader, "PATCH", f"{base_url}/api/projects/9999",
                  json={"name": "写-项目不存在"})

    r = recorder.call("create batch", leader, "POST", f"{base_url}/api/batches",
                      json={"project_id": pid, "batch_no": "W-001", "name": "写-批次甲",
                            "remark": "备注 50% ' 引号"})
    bid = r.json()["batch"]["id"]
    recorder.call("duplicate batch name", chem, "POST", f"{base_url}/api/batches",
                  json={"project_id": pid, "batch_no": "W-002", "name": "写-批次甲"})
    recorder.call("batch unknown project", leader, "POST", f"{base_url}/api/batches",
                  json={"project_id": 9999, "batch_no": "W-003", "name": "写-批次乙"})
    recorder.call("batch bad date", leader, "PATCH", f"{base_url}/api/batches/{bid}",
                  json={"synthesis_submitted_date": "2026/01/05"})
    recorder.call("batch no updatable field", leader, "PATCH", f"{base_url}/api/batches/{bid}",
                  json={})
    recorder.call("batch date forbidden for bio", bio, "PATCH", f"{base_url}/api/batches/{bid}",
                  json={"synthesis_submitted_date": "2026-02-02"})
    recorder.call("batch text by chem", chem, "PATCH", f"{base_url}/api/batches/{bid}",
                  json={"batch_no": "W-001A", "remark": "化学改的备注"})
    recorder.call("batch dates by leader", leader, "PATCH", f"{base_url}/api/batches/{bid}",
                  json={"synthesis_submitted_date": "2026-02-03",
                        "synthesis_completed_date": "2026-02-04"})

    r = recorder.call("upload file", chem, "POST", f"{base_url}/api/batches/{bid}/files",
                      files={"file_type": (None, "compound_info"),
                             "file": ("化合物信息.xlsx", b"payload-v1")})
    fid = r.json()["batch"]["files"]["compound_info"]["latest"]["id"]
    recorder.call("upload forbidden type", bio, "POST", f"{base_url}/api/batches/{bid}/files",
                  files={"file_type": (None, "compound_info"), "file": ("x.xlsx", b"data")})
    recorder.call("upload wrong extension", chem, "POST", f"{base_url}/api/batches/{bid}/files",
                  files={"file_type": (None, "compound_info"), "file": ("x.exe", b"data")})
    recorder.call("download file", leader, "GET", f"{base_url}/api/files/{fid}/download")
    recorder.call("delete file", leader, "DELETE", f"{base_url}/api/files/{fid}")
    recorder.call("delete file twice", leader, "DELETE", f"{base_url}/api/files/{fid}")
    recorder.call("restore file", leader, "POST", f"{base_url}/api/files/{fid}/restore")
    recorder.call("restore file twice", leader, "POST", f"{base_url}/api/files/{fid}/restore")

    recorder.call("delete batch", leader, "DELETE", f"{base_url}/api/batches/{bid}")
    recorder.call("delete batch twice", leader, "DELETE", f"{base_url}/api/batches/{bid}")
    recorder.call("restore batch", leader, "POST", f"{base_url}/api/batches/{bid}/restore")
    recorder.call("delete project", leader, "DELETE", f"{base_url}/api/projects/{pid}")
    recorder.call("restore project", leader, "POST", f"{base_url}/api/projects/{pid}/restore")

    users = recorder.call("list users", leader, "GET", f"{base_url}/api/users").json()["users"]
    bio5 = next(u for u in users if u["username"] == "bio5")
    recorder.call("change password wrong old", bio, "POST", f"{base_url}/api/change-password",
                  json={"old_password": "nope", "new_password": "whatever123"})
    recorder.call("reset password", leader, "POST", f"{base_url}/api/reset-password",
                  json={"user_id": bio5["id"], "new_password": "bio5new123"})
    recorder.call("reset password missing user", leader, "POST", f"{base_url}/api/reset-password",
                  json={"user_id": 9999, "new_password": "bio5new123"})
    recorder.call("login with new password", requests.Session(), "POST", f"{base_url}/api/login",
                  json={"username": "bio5", "password": "bio5new123"})
    recorder.call("login wrong password", requests.Session(), "POST", f"{base_url}/api/login",
                  json={"username": "leader", "password": "wrong"})
    recorder.call("logout", leader, "POST", f"{base_url}/api/logout")


_VIEWS = ("/api/me", "/api/users", "/api/projects", "/api/batches", "/api/trash", "/api/file-config")


def _read_snapshot(base_url):
    """三种角色各抓一遍只读接口 + 文件下载，返回可比对的结构。"""
    snap = {}
    for username, password in (
        ("leader", "labflow123"),
        ("chem1", "chem123"),
        ("bio1", "bio123"),
    ):
        session = _login(base_url, username, password)
        for path in _VIEWS:
            r = session.get(f"{base_url}{path}")
            snap[f"{username} {path}"] = [r.status_code, _normalize(r.json())]
        for batch in snap[f"{username} /api/batches"][1]["batches"]:
            for versions in batch["files"].values():
                for item in versions["versions"]:
                    r = session.get(f"{base_url}/api/files/{item['id']}/download")
                    snap[f"{username} file {item['id']}"] = [r.status_code, r.content.hex()]
    return snap


def _seed_session(base_url):
    """通过 API 造一份带历史版本、软删除和字段编辑的基础数据。"""
    leader = _login(base_url, "leader", "labflow123")
    chem = _login(base_url, "chem1", "chem123")
    bio = _login(base_url, "bio1", "bio123")

    p1 = leader.post(f"{base_url}/api/projects", json={"name": "项目甲"}).json()["project"]
    p2 = leader.post(f"{base_url}/api/projects", json={"name": "项目乙"}).json()["project"]

    b1 = leader.post(f"{base_url}/api/batches", json={
        "project_id": p1["id"], "batch_no": "BATCH-001", "name": "批次-001",
        "remark": "备注 50% ' 引号",
    }).json()["batch"]
    b2 = chem.post(f"{base_url}/api/batches", json={
        "project_id": p1["id"], "batch_no": "BATCH-002", "name": "批次-002",
    }).json()["batch"]
    b3 = leader.post(f"{base_url}/api/batches", json={
        "project_id": p2["id"], "batch_no": "BATCH-003", "name": "批次-003",
    }).json()["batch"]

    leader.patch(f"{base_url}/api/batches/{b1['id']}", json={
        "synthesis_submitted_date": "2026-01-05",
        "synthesis_completed_date": "2026-01-06",
    })
    chem.patch(f"{base_url}/api/batches/{b1['id']}", json={"batch_no": "BATCH-001A"})
    bio.patch(f"{base_url}/api/batches/{b1['id']}", json={"bio_test_start_date": "2026-01-07"})

    for session, batch_id, file_type, name, content in (
        (leader, b1["id"], "compound_info", "化合物信息.xlsx", b"v1"),
        (chem, b1["id"], "compound_info", "化合物信息-v2.xlsx", b"v2-data"),
        (bio, b1["id"], "bio_raw_data", "data.csv", b"a,b\n1,2\n"),
        (leader, b2["id"], "data_summary", "汇总.docx", b"summary"),
    ):
        r = session.post(
            f"{base_url}/api/batches/{batch_id}/files",
            files={"file_type": (None, file_type), "file": (name, content)},
        )
        assert r.status_code == 201, r.text

    leader.delete(f"{base_url}/api/batches/{b3['id']}")
    for batch in leader.get(f"{base_url}/api/batches").json()["batches"]:
        if batch["id"] == b1["id"]:
            victim = batch["files"]["compound_info"]["versions"][-1]["id"]
            leader.delete(f"{base_url}/api/files/{victim}")
            break
    else:
        raise AssertionError("批次-001 不在列表里，测试数据没造对")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server():
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), LabFlowHandler)
    server.daemon_threads = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


class TestApiParityAfterMigration:
    """迁移后所有落库接口（读 + 写）的响应与现网 sqlite 逐字一致。"""

    def test_api_responses_are_identical(self, server_url, monkeypatch):
        if db_mod.DB_BACKEND != "sqlite":
            pytest.skip("整轮用例跑在 ducklake 后端下，没有 sqlite 源库可比对")

        # 时钟固定：两次跑写脚本的时间戳一致，latest_files 那类「按时间倒序」的分组
        # 不会因为跨秒边界而重排（否则比对会 flaky）。
        monkeypatch.setattr(handler_mod, "now_iso", lambda: "2026-01-01T00:00:00+00:00")

        _seed_session(server_url)
        target = cfg.DATA_DIR / "ducklake"

        # 1) 先把 sqlite 现状（种子数据）搬进 DuckLake，作为两侧共同的起点。
        assert _run(cfg.DB_PATH, target) == mig.EXIT_OK
        assert _assert_tables_identical(cfg.DB_PATH, target)

        # 2) 在 sqlite 上跑一遍写脚本 + 读快照。
        sqlite_writes = _Recorder(server_url)
        _exercise_writes(server_url, sqlite_writes)
        sqlite_reads = _read_snapshot(server_url)

        # 3) 切到 DuckLake，起第二个实例，从同一份起点跑同样的写脚本 + 读快照。
        monkeypatch.setattr(db_mod, "DB_BACKEND", "ducklake")
        monkeypatch.setattr(handler_mod, "DB_BACKEND", "ducklake")
        db_mod.init_db()
        server, base_url = _start_server()
        try:
            ducklake_writes = _Recorder(base_url)
            _exercise_writes(base_url, ducklake_writes)
            ducklake_reads = _read_snapshot(base_url)
        finally:
            server.shutdown()
            server.server_close()
            assert db_mod._engine.pool.checkedout() == 0
            monkeypatch.setattr(db_mod, "DB_BACKEND", "sqlite")
            monkeypatch.setattr(handler_mod, "DB_BACKEND", "sqlite")
            db_mod.init_db()

        assert len(sqlite_writes.entries) >= 30, "写接口覆盖太窄"
        diff = [
            {"sqlite": a, "ducklake": b}
            for a, b in zip(sqlite_writes.entries, ducklake_writes.entries)
            if a != b
        ]
        assert not diff, json.dumps(diff, ensure_ascii=False, indent=2)

        assert set(sqlite_reads) == set(ducklake_reads)
        diff = {
            key: [sqlite_reads[key], ducklake_reads[key]]
            for key in sqlite_reads
            if sqlite_reads[key] != ducklake_reads[key]
        }
        assert not diff, json.dumps(diff, ensure_ascii=False, indent=2)
