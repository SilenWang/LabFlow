"""DL0：LabFlow 的模型层能不能原样落到 DuckLake（SQLite catalog）上。

跑法：pixi run dl0-spike        （日志写到 spikes/logs/dl0-ducklake.log）

四段，全部用仓库真实 models.py 与真实查询形状：
  1 DDL 限制   裸 duckdb 直接建表，拿 DuckLake 的原始报错（PK / UNIQUE / CHECK / FK / SEQUENCE）
  2 create_all 真实 models.py 三个版本：现状 / D1 分支 a3b7de1 / DuckLake 变体
  3 查询形状   DuckLake 变体上跑 join、回收站 subquery+outerjoin+coalesce、Query.update() 批量更新
  4 语义与代价 唯一冲突 / 外键违反是否报错；id 三种生成方式的代价与竞态

结论见 docs/DL0-DuckLake模型层核实.md。
"""

import importlib
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import duckdb
from sqlalchemy import (
    Column, Integer, Sequence, create_engine, event,
    func as sql_func, desc as sql_desc,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import ForeignKeyConstraint, PrimaryKeyConstraint, UniqueConstraint

REPO = Path(__file__).resolve().parent.parent
WORK = Path(os.environ.get("DL0_WORKDIR", "/tmp/dl0-ducklake"))
D1_REV = "a3b7de1"  # D1（VYB-415）: 主键按后端分支 + Sequence 写法
sys.path.insert(0, str(REPO))  # 让 server.config / server.models 用仓库里的那份


def h(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}", flush=True)


def run(label, fn):
    """跑一段，把成功/原始报错留给日志。"""
    try:
        got = fn()
        print(f"  [OK ] {label}" + (f" -> {got}" if got is not None else ""), flush=True)
        return True, got
    except Exception as exc:
        print(f"  [ERR] {label} -> {type(exc).__name__}: {exc}".replace("\n", " "), flush=True)
        return False, exc


# --------------------------------------------------------------------------- 1

def section1_ddl_limits():
    h("1. DuckLake 的 DDL 限制（裸 duckdb，duckdb " + duckdb.__version__ + "）")
    base = WORK / "ddl"
    base.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("LOAD ducklake; LOAD sqlite")
    con.execute(f"ATTACH 'ducklake:sqlite:{base / 'meta.sqlite'}' AS dlk (DATA_PATH '{base / 'data'}')")
    con.execute("SET search_path='dlk'")
    print("  ducklake 扩展：", con.execute(
        "select extension_name, extension_version, install_mode from duckdb_extensions() "
        "where extension_name='ducklake'").fetchall())
    run("INTEGER PRIMARY KEY", lambda: con.execute("CREATE TABLE dlk.main.t_pk (id INTEGER PRIMARY KEY, name VARCHAR)"))
    run("UNIQUE 列约束", lambda: con.execute("CREATE TABLE dlk.main.t_uq (id INTEGER, name VARCHAR UNIQUE)"))
    run("命名 UNIQUE 约束", lambda: con.execute("CREATE TABLE dlk.main.t_uq2 (id INTEGER, name VARCHAR, CONSTRAINT uq_x UNIQUE (name))"))
    run("CHECK 约束", lambda: con.execute("CREATE TABLE dlk.main.t_chk (id INTEGER CHECK (id > 0))"))
    run("CREATE SEQUENCE（DuckLake 内）", lambda: con.execute("CREATE SEQUENCE dlk.main.seq1"))
    con.execute("CREATE TABLE dlk.main.t_pk_ok (id INTEGER, name VARCHAR)")
    run("FOREIGN KEY（同 schema）", lambda: con.execute(
        "CREATE TABLE t_fk (id INTEGER, pid INTEGER REFERENCES t_pk_ok(id))"))
    run("普通表（无约束）", lambda: con.execute("CREATE TABLE dlk.main.t_ok (id INTEGER NOT NULL, name VARCHAR)"))
    run("字面量 DEFAULT", lambda: con.execute("CREATE TABLE dlk.main.t_def (id INTEGER, d VARCHAR DEFAULT '2025-01-01')"))
    run("非字面量 DEFAULT（D1 的 nextval 就是这类）", lambda: con.execute(
        "CREATE SEQUENCE main.seq_m; CREATE TABLE dlk.main.t_def2 (id INTEGER DEFAULT nextval('main.seq_m'))"))
    print("  DuckLake 里的表：", con.execute(
        "select table_name from duckdb_tables() where database_name='dlk' order by 1").fetchall())
    con.close()


# --------------------------------------------------------------------------- 2

def load_models(source, backend="duckdb"):
    """把真实 models.py 源码当模块跑，server.config 用仓库里的那份。"""
    cfg = importlib.import_module("server.config")
    cfg.DB_BACKEND = backend
    mod = types.ModuleType("dl0_models")
    exec(compile(source, "server/models.py", "exec"), mod.__dict__)
    return mod


def current_models_source():
    return (REPO / "server" / "models.py").read_text(encoding="utf-8")


def d1_models_source():
    return subprocess.run(["git", "show", f"{D1_REV}:server/models.py"], cwd=REPO,
                          capture_output=True, text=True, check=True).stdout


def ducklake_engine(db_file, catalog_dir):
    """SQLAlchemy + duckdb-engine 挂 DuckLake（SQLite catalog）。"""
    catalog_dir.mkdir(parents=True, exist_ok=True)
    # catalog 必须开 WAL：默认 delete journal 下，池里再开一条连接会读不到 snapshot
    # （实测报 "Failed to query most recent snapshot for DuckLake: database is locked"）
    import sqlite3
    with sqlite3.connect(catalog_dir / "meta.sqlite") as cat:
        cat.execute("PRAGMA journal_mode=WAL")
    engine = create_engine(f"duckdb:///{db_file}", echo=False)

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _rec):
        dbapi_connection.execute("LOAD ducklake; LOAD sqlite")
        dbapi_connection.execute(
            # IF NOT EXISTS：duckdb-engine 的多个连接会落到同一条底层连接上，
            # 无条件 ATTACH 会报 database with name "dlk" already exists
            f"ATTACH IF NOT EXISTS 'ducklake:sqlite:{catalog_dir / 'meta.sqlite'}' AS dlk "
            f"(DATA_PATH '{catalog_dir / 'data'}')")
        dbapi_connection.execute("SET search_path='dlk'")

    return engine


def ducklake_variant(d1_mod):
    """把 D1 的写法改成 DuckLake 能建的形状。

    id 不再由数据库生成（应用层给）；建表语句里不能出现 PK/UNIQUE/FK。
    ORM 那边仍要 id 当主键（否则 mapper 起不来），所以只把「建表用的元数据副本」里的
    约束摘掉，ORM 用的 Table 原样保留。
    """
    from sqlalchemy import MetaData
    ddl = MetaData()
    for table in d1_mod.Base.metadata.tables.values():
        table.to_metadata(ddl)
    for table in ddl.tables.values():
        for constraint in list(table.constraints):
            if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint, ForeignKeyConstraint)):
                table.constraints.discard(constraint)
        table.primary_key = PrimaryKeyConstraint()  # 空的主键约束不会被编进 DDL
        for col in table.columns:
            col.foreign_keys.clear()
        col = table.c.id
        col.autoincrement = False
        col.default = None
        col.server_default = None
    for table in d1_mod.Base.metadata.tables.values():
        # ORM 侧只需要「id 不由数据库生成」，约束留着不影响读写
        col = table.c.id
        col.autoincrement = False
        col.default = None
        col.server_default = None
    return d1_mod, ddl


def section2_create_all():
    h("2. Base.metadata.create_all —— 真实 models.py 三版对比")
    print(f"  SQLAlchemy {importlib.import_module('sqlalchemy').__version__}")
    for label, source in (("现状 main 的 models.py（autoincrement=True）", current_models_source()),
                          (f"D1 分支 {D1_REV} 的 models.py（Sequence + server_default=nextval）", d1_models_source())):
        mod = load_models(source)
        engine = ducklake_engine(WORK / f"c_{abs(hash(label))}.duckdb", WORK / f"cat_{abs(hash(label))}")
        run(label, lambda mod=mod, engine=engine: mod.Base.metadata.create_all(engine))
        engine.dispose()

    mod, ddl = ducklake_variant(load_models(d1_models_source()))
    engine = ducklake_engine(WORK / "c_ducklake.duckdb", WORK / "cat_ducklake")
    ok, _ = run("DuckLake 变体（id 交给应用层 + 不建 PK/UNIQUE/FK）", lambda: ddl.create_all(engine))
    if ok:
        from sqlalchemy.schema import CreateTable
        ddl_text = str(CreateTable(ddl.tables["batches"]).compile(engine))
        print("  实际建表 DDL（batches）：")
        for line in ddl_text.splitlines():
            print("   ", line)
        with engine.connect() as conn:
            print("  DuckLake 里的表：", conn.execute(
                __import__("sqlalchemy").text(
                    "select table_name from duckdb_tables() where database_name='dlk' order by 1")).fetchall())
        run("再跑一次 create_all（init_db 每次启动都会跑，checkfirst 必须幂等）",
            lambda: ddl.create_all(engine))
    return mod, ddl, engine


# --------------------------------------------------------------------------- 3

def section3_query_shapes(mod, ddl):
    h("3. 仓库真实查询形状（在 DuckLake 表上跑）")
    Base, User, Project, Batch, FileVersion = (
        mod.Base, mod.User, mod.Project, mod.Batch, mod.FileVersion)
    engine = ducklake_engine(WORK / "q.duckdb", WORK / "cat_q")
    ddl.create_all(engine)
    now = "2026-09-27T14:00:00Z"
    next_id = {}

    def app_id(session, cls):
        """没有 sequence 之后最朴素的 id 分配：max(id)+1（代价见第 4 段）。"""
        cur = session.query(sql_func.coalesce(sql_func.max(cls.id), 0)).scalar() or 0
        nxt = max(cur, next_id.get(cls.__tablename__, 0)) + 1
        next_id[cls.__tablename__] = nxt
        return nxt

    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        u1 = User(id=app_id(s, User), username="chem1", display_name="化学 1", role="chem",
                  password_salt="s", password_hash="h", active=1, created_at=now)
        u2 = User(id=app_id(s, User), username="bio1", display_name="生物 1", role="bio",
                  password_salt="s", password_hash="h", active=1, created_at=now)
        p1 = Project(id=app_id(s, Project), name="项目甲", created_by=u1.id, created_at=now)
        p2 = Project(id=app_id(s, Project), name="项目乙（回收站）", created_by=u1.id, created_at=now,
                     deleted_at=now)
        s.add_all([u1, u2, p1, p2])
        s.flush()
        b1 = Batch(id=app_id(s, Batch), project_id=p1.id, batch_no="B-001", name="批次一", remark="",
                   created_by=u1.id, created_at=now, updated_at=now)
        b2 = Batch(id=app_id(s, Batch), project_id=p1.id, batch_no="B-002", name="批次二", remark="",
                   created_by=u1.id, created_at=now, updated_at=now, deleted_at=now)
        b3 = Batch(id=app_id(s, Batch), project_id=p2.id, batch_no="B-003", name="批次三", remark="",
                   created_by=u2.id, created_at=now, updated_at=now, deleted_at=now)
        s.add_all([b1, b2, b3])
        s.flush()
        s.add(FileVersion(id=app_id(s, FileVersion), batch_id=b1.id, file_type="synthesis",
                          original_name="a.pdf", storage_path="uploads/a.pdf", size_bytes=10,
                          uploaded_by=u1.id, uploaded_at=now))
        s.commit()
        ids = {"user": u1.id, "project": p1.id, "batch": b1.id}

    with Session() as s:
        # serializers.get_batch：join + 软删过滤
        row = s.query(Batch).join(Project).filter(
            Batch.id == ids["batch"], Batch.deleted_at.is_(None),
            Project.deleted_at.is_(None)).first()
        print(f"  [OK ] get_batch 形状 -> id={row.id} name={row.name} project={row.project.name}")

        # serializers.latest_files：join + desc 排序
        files = (s.query(FileVersion).join(User)
                 .filter(FileVersion.batch_id == ids["batch"], FileVersion.deleted_at.is_(None))
                 .order_by(sql_desc(FileVersion.uploaded_at), sql_desc(FileVersion.id)).all())
        print(f"  [OK ] latest_files 形状 -> {[(f.id, f.original_name, f.uploader.display_name) for f in files]}")

        # list_batches 形状
        rows = (s.query(Batch).join(Project)
                .filter(Batch.deleted_at.is_(None), Project.deleted_at.is_(None))
                .order_by(Project.name, sql_desc(Batch.created_at), sql_desc(Batch.id)).all())
        print(f"  [OK ] list_batches 形状 -> {[(r.id, r.name) for r in rows]}")

        # handler.list_trash：subquery + outerjoin + coalesce
        subq = s.query(Batch.project_id, sql_func.count(Batch.id).label("cnt")).filter(
            Batch.deleted_at.isnot(None)).group_by(Batch.project_id).subquery()
        trash = (s.query(Project.id, Project.name, Project.deleted_at,
                         sql_func.coalesce(subq.c.cnt, 0).label("deleted_batch_count"))
                 .outerjoin(subq, Project.id == subq.c.project_id)
                 .filter(Project.deleted_at.isnot(None))
                 .order_by(sql_desc(Project.deleted_at), sql_desc(Project.id)).all())
        print(f"  [OK ] 回收站 subquery+outerjoin+coalesce -> "
              f"{[(r.id, r.name, r.deleted_batch_count) for r in trash]}")

        # handler.delete_project：Query.update() 批量软删
        n = s.query(Batch).filter(Batch.project_id == ids["project"],
                                  Batch.deleted_at.is_(None)).update({Batch.deleted_at: now})
        s.commit()
        left = s.query(Batch).filter(Batch.project_id == ids["project"],
                                     Batch.deleted_at.is_(None)).count()
        print(f"  [OK ] Query.update() 批量软删 -> update 返回 rowcount={n}（duckdb-engine 是 -1），剩余未删={left}")
    engine.dispose()


# --------------------------------------------------------------------------- 4

def section4_semantics_and_cost(mod, ddl):
    h("4. 唯一 / 外键是否真生效；id 生成三种方式的代价")
    Project, Batch = mod.Project, mod.Batch
    engine = ducklake_engine(WORK / "sem.duckdb", WORK / "cat_sem")
    ddl.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    now = "2026-09-27T14:00:00Z"

    with Session() as s:
        p = Project(id=1, name="项目甲", created_by=1, created_at=now)
        s.add(p)
        s.add(Batch(id=1, project_id=1, batch_no="B-001", name="批次一", created_by=1,
                    created_at=now, updated_at=now))
        s.commit()
        # 同名批次（现有语义：全系统唯一，含回收站）
        s2 = Session()
        s2.add(Batch(id=2, project_id=1, batch_no="B-002", name="批次一", created_by=1,
                     created_at=now, updated_at=now))
        ok, exc = run("插入重名批次（现有语义应当 409）", s2.commit)
        print(f"       -> IntegrityError={isinstance(exc, IntegrityError)}；库里同名行数="
              f"{Session().query(Batch).filter(Batch.name == '批次一').count()}")
        s2.close()
        # 孤儿外键
        s3 = Session()
        s3.add(Batch(id=3, project_id=999, batch_no="B-003", name="批次三", created_by=1,
                     created_at=now, updated_at=now))
        ok, exc = run("插入 project_id=999 的孤儿批次（现有语义应当被拒）", s3.commit)
        print(f"       -> IntegrityError={isinstance(exc, IntegrityError)}；孤儿行数="
              f"{Session().query(Batch).filter(Batch.project_id == 999).count()}")
        s3.close()
        # 重复 id
        s4 = Session()
        s4.add(Batch(id=1, project_id=1, batch_no="B-004", name="批次四", created_by=1,
                     created_at=now, updated_at=now))
        run("插入重复主键 id=1（现有语义应当被拒）", s4.commit)
        s4.close()
        print("       -> id=1 的行数=", Session().query(Batch).filter(Batch.id == 1).count())

    # (a) max(id)+1
    n = 30
    with Session() as s:
        t0 = time.perf_counter()
        for i in range(n):
            rid = (s.query(sql_func.coalesce(sql_func.max(Batch.id), 0)).scalar() or 0) + 1
            s.add(Batch(id=rid, project_id=1, batch_no=f"X{i}", name=f"x{i}", created_by=1,
                        created_at=now, updated_at=now))
            s.commit()
        print(f"  [OK ] (a) max(id)+1 + INSERT：{n} 次 / {(time.perf_counter() - t0) * 1000 / n:.1f} ms 每次")
    with Session() as s:
        a = (s.query(sql_func.coalesce(sql_func.max(Batch.id), 0)).scalar() or 0)
        print(f"       -> 竞态示例：会话 A 读到 max={a}，会话 B 同时读到 max={a}，两边都插入 id={a + 1}"
              f"（DuckLake 无唯一约束，落库是两条同 id 的行）")

    # (b) DuckLake 里的计数表
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE dlk.main.id_counter (k VARCHAR, v INTEGER)"))
        conn.execute(text("INSERT INTO dlk.main.id_counter VALUES ('batches', 0)"))
    got = []
    try:
        with engine.connect() as conn:
            t0 = time.perf_counter()
            for _ in range(n):
                got.append(conn.execute(text(
                    "UPDATE dlk.main.id_counter SET v = v + 1 WHERE k = 'batches' RETURNING v")).scalar())
        print(f"  [OK ] (b) DuckLake 计数表 UPDATE ... RETURNING：{n} 次 / "
              f"{(time.perf_counter() - t0) * 1000 / n:.1f} ms 每次，区间 {got[0]}..{got[-1]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [ERR] (b) DuckLake 计数表 UPDATE ... RETURNING -> {type(exc).__name__}: {exc}")
        got = []
        with engine.connect() as conn:
            t0 = time.perf_counter()
            for _ in range(n):
                conn.execute(text("UPDATE dlk.main.id_counter SET v = v + 1 WHERE k = 'batches'"))
                got.append(conn.execute(text("SELECT v FROM dlk.main.id_counter WHERE k = 'batches'")).scalar())
        print(f"       -> 退化成 UPDATE + SELECT 两条语句：{n} 次 / "
              f"{(time.perf_counter() - t0) * 1000 / n:.1f} ms 每次，区间 {got[0]}..{got[-1]}")

    # (c) 辅助 SQLite 计数表（catalog 本来就是 SQLite，再加一张自增表几乎不花钱）
    aux = WORK / "ids.sqlite"
    with sqlite3.connect(aux, timeout=30) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE IF NOT EXISTS seq (name TEXT PRIMARY KEY, next INTEGER)")

    def alloc(conn):
        cur = conn.execute(
            "INSERT INTO seq(name, next) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET next = next + 1 RETURNING next", ("batches",))
        return cur.fetchone()[0]

    def open_aux():
        conn = sqlite3.connect(aux, timeout=30)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    con = open_aux()
    t0 = time.perf_counter()
    single = [alloc(con) for _ in range(200)]
    con.commit()
    print(f"  [OK ] (c) 辅助 SQLite 计数表：200 次 / {(time.perf_counter() - t0) * 1000 / 200:.3f} ms 每次；"
          f"区间 {single[0]}..{single[-1]}，唯一 {len(set(single))}/200")
    con.close()

    batches, errs = [], []

    def worker():
        try:
            c = open_aux()
            values = [alloc(c) for _ in range(100)]
            c.commit()
            c.close()
            batches.append(values)
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    got = [v for values in batches for v in values]
    print(f"       -> 4 线程各取 100 个 id：拿到 {len(got)} 个，唯一 {len(set(got))}，报错 {len(errs)} 个"
          + (f"（{type(errs[0]).__name__}: {errs[0]}）" if errs else ""))

    # 应用层唯一校验的形状与代价（DuckLake 没有 DB 级唯一约束，409 只能自己查）
    with Session() as s:
        t0 = time.perf_counter()
        for _ in range(30):
            s.query(Batch.id).filter(Batch.name == "批次一").first()
        print(f"  [OK ] 应用层唯一校验 SELECT 1 FROM batches WHERE name=?：30 次 / "
              f"{(time.perf_counter() - t0) * 1000 / 30:.2f} ms 每次；"
              f"竞态窗口 = 该校验与 INSERT 提交之间（无 DB 兜底，两进程可同时通过）")
        # session() 的错误路径：出错后 rollback，连接还能不能继续用
        from sqlalchemy import text
        try:
            s.execute(text("INSERT INTO dlk.main.batches (id, project_id, batch_no, name, "
                           "created_by, created_at, updated_at) VALUES (901, 1, 'Z', 'zz', 1, NULL, NULL)"))
            print("  [ERR] NOT NULL 违反没有被拦下")
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            print(f"  [OK ] NOT NULL 违反照常报错 -> {type(exc).__name__}；rollback 后 ", end="")
            s.add(Batch(id=902, project_id=1, batch_no="B-902", name="批次902", created_by=1,
                        created_at=now, updated_at=now))
            s.commit()
            print(f"继续插入成功（id=902 行数="
                  f"{s.query(Batch).filter(Batch.id == 902).count()}）")
    engine.dispose()


# --------------------------------------------------------------------------- 5

def cleanup():
    shutil.rmtree(WORK, ignore_errors=True)


def main():
    cleanup()  # 每次从干净目录起，避免上次的 catalog / 表残留
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"duckdb {duckdb.__version__} / sqlalchemy "
          f"{importlib.import_module('sqlalchemy').__version__} / python {sys.version.split()[0]}")
    section1_ddl_limits()
    mod, ddl, engine = section2_create_all()
    engine.dispose()
    if mod is not None:
        section3_query_shapes(mod, ddl)
        section4_semantics_and_cost(mod, ddl)
    cleanup()


if __name__ == "__main__":
    main()
