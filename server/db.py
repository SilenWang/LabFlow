import atexit
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import MetaData, create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import ForeignKeyConstraint, PrimaryKeyConstraint, UniqueConstraint

from server.auth import password_hash
from server.config import DB_BACKEND, DB_PATH
from server.models import Base, Batch, FileVersion, Project, User
from server.utils import ensure_dirs, now_iso

_engine = None
_Session = None
_seekdb_instance = None

# DB_BACKEND 定义在 config.py（models.py 也要用来选主键写法），这里沿用同名。
# sqlite 为默认后端，便于回滚；LABFLOW_DB=seekdb|ducklake 切到对应后端。
# 列长必须显式写在 models 里：seekdb 走 MySQL 协议，裸 VARCHAR 会被直接拒。

SEEKDB_DATABASE = "labflow"
DUCKLAKE_ALIAS = "dlk"


def _seekdb_dir():
    # 跟着 DB_PATH 走，测试里按用例切换临时目录时自动隔离。
    return Path(DB_PATH).parent / "seekdb"


def _ducklake_dir():
    # 同理跟着 DB_PATH 走：sqlite 是单个 data/labflow.db，
    # DuckLake 是 data/ducklake/{catalog.sqlite,data/,client.duckdb,ids.sqlite}。
    return Path(DB_PATH).parent / "ducklake"


def _ducklake_client_path():
    # 应用侧那条 DuckDB 连接挂载用的空文件（数据都在 catalog + parquet 里），
    # 放在 ducklake 目录内，免得被当成"数据库"单独备份或让 MCP 去开。
    return _ducklake_dir() / "client.duckdb"


def _ducklake_catalog_path():
    return _ducklake_dir() / "catalog.sqlite"


def make_ducklake_engine():
    """duckdb-engine + DuckLake（SQLite catalog）。

    DuckLake 建不了 PK/UNIQUE/FK、也没有 sequence（DL0 实测），所以这里只负责
    「把文件挂上、让建表语句能编出来」；id 分配与唯一性校验分别见 next_id() 与 handler。
    """
    data_dir = _ducklake_dir() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog = _ducklake_catalog_path()
    # catalog 必须开 WAL：默认 delete journal 时，池里第二条连接读 snapshot 会报
    # "Failed to query most recent snapshot for DuckLake: database is locked"。
    with sqlite3.connect(catalog) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    engine = create_engine(f"duckdb:///{_ducklake_client_path()}", echo=False)

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, connection_record):
        dbapi_connection.execute("LOAD ducklake; LOAD sqlite")
        # IF NOT EXISTS：池里给同一个 DuckDB 文件再开连接时 dlk 已经挂过，
        # 无条件 ATTACH 会报 database with name "dlk" already exists。
        dbapi_connection.execute(
            f"ATTACH IF NOT EXISTS 'ducklake:sqlite:{catalog}' AS {DUCKLAKE_ALIAS} "
            f"(DATA_PATH '{data_dir}')"
        )
        dbapi_connection.execute(f"SET search_path='{DUCKLAKE_ALIAS}'")

    return engine


def _ducklake_ddl_metadata():
    """建表用的元数据副本：把 PK/UNIQUE/FK 摘掉（DuckLake 只认 NOT NULL）。

    ORM 那边的 Table 保持原样（mapper 必须有主键，关系也要留着），只有建表走副本。
    """
    ddl = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(ddl)
    for table in ddl.tables.values():
        for constraint in list(table.constraints):
            if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint, ForeignKeyConstraint)):
                table.constraints.discard(constraint)
        table.primary_key = PrimaryKeyConstraint()  # 空的主键约束不会被编进 CREATE TABLE
        for column in table.columns:
            column.foreign_keys.clear()
    return ddl


def create_all(engine):
    if DB_BACKEND == "ducklake":
        _ducklake_ddl_metadata().create_all(engine)
    else:
        Base.metadata.create_all(engine)


def next_id(table_name):
    """DuckLake 没有 sequence，id 由应用层从这里取。

    catalog 本身就是 SQLite，加一张计数器表几乎不花钱：WAL + busy_timeout 下
    多线程/多进程取号不重复（DL0 实测 4 线程各 100 个 = 400/400 唯一）。
    ponytail: 每次取号开一条 SQLite 连接（~0.04ms）；要提速再考虑连接复用。
    """
    with sqlite3.connect(_ducklake_dir() / "ids.sqlite", timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("CREATE TABLE IF NOT EXISTS id_seq (name TEXT PRIMARY KEY, next INTEGER)")
        row = conn.execute(
            "INSERT INTO id_seq (name, next) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET next = next + 1 RETURNING next",
            (table_name,),
        ).fetchone()
        return row[0]


def _assign_app_id(mapper, connection, target):
    if target.id is None:
        target.id = next_id(mapper.class_.__tablename__)


# 只在 DuckLake 下挂：其它后端由数据库自己给 id。
# 挂在 mapper 上（而不是 handler 的建对象处），所有 ORM 插入都在 flush 前拿到号。
if DB_BACKEND == "ducklake":
    for _model in (User, Project, Batch, FileVersion):
        event.listen(_model, "before_insert", _assign_app_id)


def open_seekdb(db_dir=None, database=SEEKDB_DATABASE):
    """打开嵌入式 seekdb 实例，返回 (instance, pymysql 连接参数)。

    db_dir 缺省跟随 DB_PATH；迁移脚本要显式指定目标目录，故暴露为公共入口。
    """
    global _seekdb_instance
    import pymysql
    import pylibseekdb as seekdb

    db_dir = Path(db_dir) if db_dir is not None else _seekdb_dir()
    db_dir.mkdir(parents=True, exist_ok=True)
    _seekdb_instance = seekdb.open(db_dir=str(db_dir))
    opts = dict(_seekdb_instance.connection_options())
    conn = pymysql.connect(**opts, charset="utf8mb4", autocommit=True)
    try:
        conn.cursor().execute(f"CREATE DATABASE IF NOT EXISTS `{database}`")
    finally:
        conn.close()
    return _seekdb_instance, opts


def close_seekdb():
    global _seekdb_instance
    instance, _seekdb_instance = _seekdb_instance, None
    if instance is None:
        return
    instance.close()
    # pylibseekdb 的嵌入式服务由 C 库直接 fork 出子进程，close() 只让它退出，
    # 之后会变成僵尸；Python 不会自动回收。这里兜底 reap，避免残留子进程。
    for _ in range(20):
        try:
            if os.waitpid(-1, os.WNOHANG)[0] != 0:
                continue
        except ChildProcessError:
            return
        time.sleep(0.01)


def _shutdown():
    global _engine
    if _engine is not None:
        _engine.dispose()
    close_seekdb()


# 进程退出（含异常路径）时释放 seekdb，避免残留实例/子进程。
atexit.register(_shutdown)


def get_engine():
    global _engine
    if _engine is None:
        if DB_BACKEND == "seekdb":
            _, opts = open_seekdb()
            _engine = create_engine(
                f"mysql+pymysql://root@localhost/{SEEKDB_DATABASE}",
                echo=False,
                connect_args={**opts, "charset": "utf8mb4"},
            )
        elif DB_BACKEND == "ducklake":
            _engine = make_ducklake_engine()
        else:
            _engine = create_engine(
                f"sqlite:///{DB_PATH}",
                echo=False,
                connect_args={"check_same_thread": False},
            )

            @event.listens_for(_engine, "connect")
            def _set_foreign_keys(dbapi_connection, connection_record):
                dbapi_connection.execute("PRAGMA foreign_keys = ON")
    return _engine


def make_session():
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _Session()


@contextmanager
def session():
    s = make_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db():
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    close_seekdb()
    _engine = None
    _Session = None
    ensure_dirs()
    create_all(get_engine())
    migrate_schema()
    with session() as s:
        count = s.query(User).count()
        if count == 0:
            seed_users(s)


def migrate_schema():
    # 结构迁移退场：新库由 create_all 一次成型。仅 sqlite 历史库需要补 remark 列。
    if DB_BACKEND != "sqlite":
        return
    engine = get_engine()
    inspector = inspect(engine)
    if "batches" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("batches")]
    if "remark" not in columns:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE batches ADD COLUMN remark TEXT"))
            conn.commit()


def seed_users(s):
    defaults = [
        ("leader", "总负责人", "manager", "labflow123"),
        ("chem1", "化学 1", "chem", "chem123"),
        ("chem2", "化学 2", "chem", "chem123"),
        ("chem3", "化学 3", "chem", "chem123"),
        ("bio1", "生物 1", "bio", "bio123"),
        ("bio2", "生物 2", "bio", "bio123"),
        ("bio3", "生物 3", "bio", "bio123"),
        ("bio4", "生物 4", "bio", "bio123"),
        ("bio5", "生物 5", "bio", "bio123"),
    ]
    for username, display_name, role, password in defaults:
        salt, digest = password_hash(password)
        s.add(User(
            username=username,
            display_name=display_name,
            role=role,
            password_salt=salt,
            password_hash=digest,
            active=1,
            created_at=now_iso(),
        ))
