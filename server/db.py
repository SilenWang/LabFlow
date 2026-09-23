import atexit
import os
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import String, create_engine, event, inspect, text
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from server.auth import password_hash
from server.config import DB_PATH
from server.models import Base, User
from server.utils import ensure_dirs, now_iso

_engine = None
_Session = None
_seekdb_instance = None

# sqlite 为默认后端，便于回滚；LABFLOW_DB=seekdb 切到嵌入式 seekdb。
DB_BACKEND = os.environ.get("LABFLOW_DB", "sqlite").strip().lower()


@compiles(String, "mysql")
def _mysql_string(element, compiler, **kw):
    # seekdb 走 MySQL 协议：裸 VARCHAR（未指定长度）会被拒，而 TEXT 又不能建索引
    # （unique 列会报 "storage engine can't index"）。未指定长度的 String 按 MySQL
    # 惯例落成 VARCHAR(255)，这样不必改动 models 里的列定义。
    if element.length is None:
        return "VARCHAR(255)"
    return compiler.visit_VARCHAR(element, **kw)


def _seekdb_dir():
    # 跟着 DB_PATH 走，测试里按用例切换临时目录时自动隔离。
    return Path(DB_PATH).parent / "seekdb"


def _open_seekdb():
    """打开嵌入式 seekdb 实例，返回 (instance, pymysql 连接参数)。"""
    global _seekdb_instance
    import pymysql
    import pylibseekdb as seekdb

    _seekdb_dir().mkdir(parents=True, exist_ok=True)
    _seekdb_instance = seekdb.open(db_dir=str(_seekdb_dir()))
    opts = dict(_seekdb_instance.connection_options())
    conn = pymysql.connect(**opts, charset="utf8mb4", autocommit=True)
    try:
        conn.cursor().execute("CREATE DATABASE IF NOT EXISTS labflow")
    finally:
        conn.close()
    return _seekdb_instance, opts


def _close_seekdb():
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
    _close_seekdb()


# 进程退出（含异常路径）时释放 seekdb，避免残留实例/子进程。
atexit.register(_shutdown)


def get_engine():
    global _engine
    if _engine is None:
        if DB_BACKEND == "seekdb":
            _, opts = _open_seekdb()
            _engine = create_engine(
                "mysql+pymysql://root@localhost/labflow",
                echo=False,
                connect_args={**opts, "charset": "utf8mb4"},
            )
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
    _close_seekdb()
    _engine = None
    _Session = None
    ensure_dirs()
    Base.metadata.create_all(get_engine())
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
