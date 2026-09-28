import atexit
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import MetaData, create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import ForeignKeyConstraint, PrimaryKeyConstraint, UniqueConstraint

from server.auth import password_hash
from server.config import DB_BACKEND, DB_PATH
from server.models import Base, Batch, FileVersion, Project, User
from server.utils import ensure_dirs, now_iso

_engine = None
_Session = None
_attach_lock = threading.Lock()
# 同进程的写提交串行化：DuckLake 的提交要抢 catalog 的 SQLite 写锁，多个线程
# 同时提交会互相撞（DL1 实测连接池 3 线程下 74.3%）。串行之后进程内基本不撞，
# 剩下的是跨进程冲突，交给下面的退避重试。只罩住 flush+commit，读不受影响。
_commit_lock = threading.Lock()

# DuckLake 的写提交冲突（catalog 是 SQLite 单写者）只能靠应用层退避重试兜：
# 两个事务同时提交时，后者报 "Failed to commit DuckLake transaction ... database
# is locked"，引擎自带的 ducklake_max_retry_count 那三个配置覆盖不到这个场景
# （DL1 实测 0/10，且 22–35ms 就返回、没有 exceeded max retry count 文案）。
# 重试落在 session() 收尾处，见 _commit_with_retry()。退避给到 3 秒以上：外部进程
# 长时间占着 catalog 时（验收探针压 1.5s）也要能撑到锁释放，别把重试变成 500。
COMMIT_RETRY_ATTEMPTS = int(os.environ.get("LABFLOW_COMMIT_RETRY_ATTEMPTS", "6"))
COMMIT_RETRY_BASE_DELAY = 0.2  # 秒，按 COMMIT_RETRY_BACKOFF 翻倍、封顶 COMMIT_RETRY_MAX_DELAY
COMMIT_RETRY_BACKOFF = 2.0
COMMIT_RETRY_MAX_DELAY = 1.0
_COUNTER_BUSY_TIMEOUT_MS = 15000  # 应用层 id 计数器排队等的上限

# 只重试"提交撞上 catalog 写锁 / 旧快照"这一类，业务错误（唯一约束、校验失败）原样抛出。
_RETRYABLE_COMMIT_MARKERS = (
    "Failed to commit DuckLake transaction",
    "Failed to flush changes into DuckLake",
    "Conflict on update",
    "TransactionContext Error: Failed to commit",
)
_REPLAY_KEY = "ducklake_replay"
_WRITE_FLAG = "ducklake_wrote"
_CORE_DML_KEY = "ducklake_core_dml"
_REPLAYING = "ducklake_replaying"
_CLAIMS_KEY = "ducklake_name_claims"

# DuckLake 表建不了 UNIQUE（DL0 实测），名字唯一性只能下沉到辅助 SQLite：占位表带
# UNIQUE，插入是跨进程/跨线程原子的，"检查过 → 插入"之间没有窗口。占位与 DuckLake
# 写入不算同一个事务（两个库），所以写失败/回滚时按 session 里的日志放掉占位。
_NAME_CLAIMS_DDL = (
    "CREATE TABLE IF NOT EXISTS name_claims ("
    "scope TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (scope, name))"
)

# DB_BACKEND 定义在 config.py（models.py 也要用来选主键写法），这里沿用同名。
# sqlite 为默认后端，便于回滚；LABFLOW_DB=ducklake 切到 DuckLake。

DUCKLAKE_ALIAS = "dlk"


def _ducklake_dir(ducklake_dir=None):
    # 同理跟着 DB_PATH 走：sqlite 是单个 data/labflow.db，
    # DuckLake 是 data/ducklake/{catalog.sqlite,data/,client.duckdb,ids.sqlite}。
    # 显式传入 ducklake_dir 时按传入的来：迁移脚本要写到指定目标目录。
    # 一律取绝对路径：DuckDB 同进程按主库路径字符串共享实例，相对/绝对会被当成两个库。
    if ducklake_dir is not None:
        return Path(ducklake_dir).resolve()
    return (Path(DB_PATH).parent / "ducklake").resolve()


def _ducklake_client_path(ducklake_dir=None):
    # 应用侧那条 DuckDB 连接挂载用的空文件（数据都在 catalog + parquet 里），
    # 放在 ducklake 目录内，免得被当成"数据库"单独备份或让 MCP 去开。
    return _ducklake_dir(ducklake_dir) / "client.duckdb"


def _ducklake_catalog_path(ducklake_dir=None):
    return _ducklake_dir(ducklake_dir) / "catalog.sqlite"


def make_ducklake_engine(ducklake_dir=None):
    """duckdb-engine + DuckLake（SQLite catalog）。

    DuckLake 建不了 PK/UNIQUE/FK、也没有 sequence（DL0 实测），所以这里只负责
    「把文件挂上、让建表语句能编出来」；id 分配与唯一性校验分别见 next_id() 与 handler。
    """
    data_dir = _ducklake_dir(ducklake_dir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog = _ducklake_catalog_path(ducklake_dir)
    # catalog 必须开 WAL：默认 delete journal 时，池里第二条连接读 snapshot 会报
    # "Failed to query most recent snapshot for DuckLake: database is locked"。
    with sqlite3.connect(catalog) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    engine = create_engine(f"duckdb:///{_ducklake_client_path(ducklake_dir)}", echo=False)

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, connection_record):
        # 每条池连接都要 LOAD + SET search_path（search_path 是连接级的，实测），
        # 但 ATTACH 必须"每进程一次、池内复用"：DuckDB 同进程按主库路径共享实例，
        # 别名一旦挂上其他连接直接可见；每条连接都 ATTACH 会报
        # "Binder Error: Failed to attach database: database with name "dlk"
        # already exists"（DL1 实测连接池下 67.6% 成功率）。
        dbapi_connection.execute("LOAD ducklake; LOAD sqlite")
        with _attach_lock:
            if not _alias_attached(dbapi_connection, DUCKLAKE_ALIAS):
                try:
                    # OVERRIDE_DATA_PATH：catalog 里记着建库时的绝对数据路径，备份
                    # 还原到别的目录后（灾难恢复到另一台机器）路径对不上会拒绝挂载。
                    # 我们的 data/ 永远是 catalog 的同级目录，用这个开关让目录可整体搬移。
                    dbapi_connection.execute(
                        f"ATTACH 'ducklake:sqlite:{catalog}' AS {DUCKLAKE_ALIAS} "
                        f"(DATA_PATH '{data_dir}', OVERRIDE_DATA_PATH true)"
                    )
                except Exception as exc:
                    # 并发/同进程重建引擎时别名可能刚被挂上，容忍这一类重名错误。
                    if "already exists" not in str(exc):
                        raise
        dbapi_connection.execute(f"SET search_path='{DUCKLAKE_ALIAS}'")

    return engine


def _alias_attached(dbapi_connection, alias):
    """池里这条连接所在 DuckDB 实例上，别名是否已经挂过。"""
    row = dbapi_connection.execute(
        "SELECT 1 FROM duckdb_databases() WHERE database_name = ?", (alias,)
    ).fetchone()
    return row is not None


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


def create_ducklake_tables(engine):
    """按 DuckLake 的建表规则建表（与 LABFLOW_DB 当前取值无关）。

    迁移脚本要在 sqlite 配置下把表建进 DuckLake，所以这一步不能走 ``create_all``
    那条按后端分支的路；``create_all`` 自己也复用本函数。
    """
    _ducklake_ddl_metadata().create_all(engine)


def create_all(engine):
    if DB_BACKEND == "ducklake":
        create_ducklake_tables(engine)
    else:
        Base.metadata.create_all(engine)


def next_id(table_name, ducklake_dir=None):
    """DuckLake 没有 sequence，id 由应用层从这里取。

    catalog 本身就是 SQLite，加一张计数器表几乎不花钱：BEGIN IMMEDIATE +
    busy_timeout 下多线程/多进程取号不重复（DL0 实测 4 线程各 100 个 = 400/400 唯一）。
    存的是"最后一个已发出的号"、先自增再返回：`reset_id_counter(表, max_id)`（D3 迁移）
    与 `resync_id_counters()`（启动）都按同一语义写。
    ponytail: 每次取号开一条 SQLite 连接（~0.04ms）；要提速再考虑连接复用。
    """
    conn = _counter_conn(ducklake_dir)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS id_seq (name TEXT PRIMARY KEY, next INTEGER)")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO id_seq (name, next) VALUES (?, 0) ON CONFLICT(name) DO NOTHING",
                (table_name,),
            )
            row = conn.execute(
                "UPDATE id_seq SET next = next + 1 WHERE name = ? RETURNING next",
                (table_name,),
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return row[0]


def _ducklake_ids_path(ducklake_dir=None):
    return _ducklake_dir(ducklake_dir) / "ids.sqlite"


def _counter_conn(ducklake_dir=None):
    """辅助计数库的连接：autocommit + 显式 BEGIN IMMEDIATE。

    sqlite3 默认的 deferred 事务在"写锁已被别人拿着"时升级会直接返回 SQLITE_BUSY，
    连 busy_timeout 都不走（SQLite 的防死锁路径），并发取号会偶发 database is
    locked；BEGIN IMMEDIATE 先拿写锁，后来的老实排队等，而不是报错。
    """
    conn = sqlite3.connect(_ducklake_ids_path(ducklake_dir), timeout=30, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={_COUNTER_BUSY_TIMEOUT_MS}")
    return conn


def resync_id_counters(s=None):
    """把 id 计数器对齐到「库里已用掉的最大 id」，启动与迁移导入之后都要跑。

    迁移脚本会带历史 id 显式导入，而计数器默认从 1 开始——不续号的话第一条新
    数据就撞历史 id（D0 预研实测：导入 id=7 后下一行拿到 1）。这里取各表 max(id)
    与现值的大者写回，多跑几次也不会倒退；下一次 next_id() 拿到 max(id)+1。

    返回 {表名: 已发出的最大 id}，供调用方（迁移脚本）核对。
    """
    if DB_BACKEND != "ducklake":
        return {}
    tables = [model.__tablename__ for model in (User, Project, Batch, FileVersion)]
    owns_session = s is None
    if owns_session:
        s = make_session()
    try:
        counters = {}
        for table in tables:
            max_id = s.execute(text(f"SELECT max(id) FROM {table}")).scalar()
            counters[table] = int(max_id or 0)
    finally:
        if owns_session:
            s.close()
    conn = _counter_conn()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS id_seq (name TEXT PRIMARY KEY, next INTEGER)")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                "INSERT INTO id_seq (name, next) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET next = MAX(next, excluded.next)",
                list(counters.items()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return counters


def claim_name(sess, scope, name):
    """抢占一个名字，撞 UNIQUE 返回 False（调用方转 409）。

    scope 就是冲突文案的 key（``projects.name`` / ``batches.name``），作用域与大小写
    语义跟原来的 ``assert_name_unique`` 一致：全表唯一、含回收站、区分大小写
    （辅助 SQLite 默认 BINARY 排序，与 DuckLake 的 VARCHAR 比较一致）。
    """
    if DB_BACKEND != "ducklake":
        return True
    conn = _counter_conn()
    try:
        conn.execute(_NAME_CLAIMS_DDL)
        try:
            conn.execute("INSERT INTO name_claims (scope, name) VALUES (?, ?)", (scope, name))
        except sqlite3.IntegrityError:
            return False
    finally:
        conn.close()
    _journal_claim(sess, ("claim", scope, name))
    return True


def release_name(sess, scope, name):
    """放掉一个名字占位（改名时先放旧名）；写失败/回滚会按日志放回去。"""
    if DB_BACKEND != "ducklake":
        return
    conn = _counter_conn()
    try:
        conn.execute(_NAME_CLAIMS_DDL)
        conn.execute("DELETE FROM name_claims WHERE scope = ? AND name = ?", (scope, name))
    finally:
        conn.close()
    _journal_claim(sess, ("release", scope, name))


def resync_name_claims(s=None):
    """把名字占位表对齐到库里的真实数据：启动与迁移导入之后都要跑。

    库里已有的名字补上占位（老库、迁移导入）；没有对应行的孤儿占位清掉——进程如果
    崩在"占了位还没写库"之间会留下这种占位，不清的话那个名字就永远用不了。
    占位表由应用独占使用（DuckDB 的进程独占锁保证只有一个写进程），启动时重建安全。
    """
    if DB_BACKEND != "ducklake":
        return {}
    owns_session = s is None
    if owns_session:
        s = make_session()
    try:
        claims = {}
        for model in (Project, Batch):
            scope = f"{model.__tablename__}.name"
            claims[scope] = [name for (name,) in s.query(model.name).all() if name]
    finally:
        if owns_session:
            s.close()
    conn = _counter_conn()
    try:
        conn.execute(_NAME_CLAIMS_DDL)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM name_claims")
            conn.executemany(
                "INSERT OR IGNORE INTO name_claims (scope, name) VALUES (?, ?)",
                [(scope, name) for scope, names in claims.items() for name in names],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {scope: len(names) for scope, names in claims.items()}


def _journal_claim(sess, entry):
    sess.info.setdefault(_CLAIMS_KEY, []).append(entry)


def _undo_claims(sess):
    """把这笔会话里占/放的名字占位回退掉（提交失败、业务异常时）。"""
    journal = sess.info.pop(_CLAIMS_KEY, None)
    if not journal or DB_BACKEND != "ducklake":
        return
    conn = _counter_conn()
    try:
        conn.execute(_NAME_CLAIMS_DDL)
        for action, scope, name in reversed(journal):
            if action == "claim":
                conn.execute("DELETE FROM name_claims WHERE scope = ? AND name = ?", (scope, name))
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO name_claims (scope, name) VALUES (?, ?)", (scope, name)
                )
    finally:
        conn.close()


def _column_values(obj):
    return {column.key: getattr(obj, column.key) for column in obj.__table__.columns}


def _capture_flush(sess, flush_context, instances):
    """flush 前记下本次要写的对象，提交冲突回滚后用它重放。

    rollback 会把新对象 expunge 出会话、把改过的属性 expire（改动消失），所以
    "再 commit 一次"会静默丢写。把 flush 里的新增/修改/删除攒在 session.info
    上，重试前重新放回会话，重放的就是同一批写。
    """
    if DB_BACKEND != "ducklake":
        return
    if sess.new or sess.dirty or sess.deleted:
        sess.info[_WRITE_FLAG] = True
    replay = sess.info.setdefault(_REPLAY_KEY, {})
    for obj in sess.new:
        replay[obj] = ("insert", _column_values(obj))
    for obj in sess.dirty:
        if obj not in replay:
            replay[obj] = ("update", _column_values(obj))
    for obj in sess.deleted:
        replay[obj] = ("delete", _column_values(obj))


def _replay_flush(sess):
    # 先放回 handler 自己 execute 的核心 DML（Query.update 那类批量写），再让 ORM
    # 重新 flush 待写对象；两者都缺了就是丢写。
    sess.info[_REPLAYING] = True
    try:
        for statement, parameters in list(sess.info.get(_CORE_DML_KEY) or []):
            sess.execute(statement, parameters)
        for obj, (op, values) in (sess.info.get(_REPLAY_KEY) or {}).items():
            if op == "insert":
                sess.add(obj)
            for key, value in values.items():
                setattr(obj, key, value)
            if op == "delete":
                sess.delete(obj)
    finally:
        sess.info.pop(_REPLAYING, None)


def _capture_core_dml(orm_execute_state):
    """记下 handler 直接用 session.execute / Query.update 发出的写语句。

    这些语句不走 ORM flush，before_flush 记不到；不记下来，提交冲突重试时会把它
    整条丢掉（例如 delete_project 里批量软删批次的 UPDATE）。
    """
    if DB_BACKEND != "ducklake":
        return
    state = orm_execute_state
    if not (state.is_insert or state.is_update or state.is_delete):
        return
    sess = state.session
    if sess.info.get(_REPLAYING):
        return
    sess.info.setdefault(_CORE_DML_KEY, []).append(
        (state.statement, dict(state.parameters or {}))
    )
    sess.info[_WRITE_FLAG] = True


def _refresh_expired(sess):
    """重试前那次 rollback 会把会话里所有对象 expire（连没改过的也一起）。

    handler 常在 with 块之后接着读这些对象（例如 serialize_batch 里的
    batch.project.name），不刷回来会在已关闭的会话上抛 DetachedInstanceError。
    """
    for obj in list(sess.identity_map.values()):
        if inspect(obj).expired:
            try:
                sess.refresh(obj)
            except Exception:  # pragma: no cover - 行已不在（被删除）时保持原样
                continue


def is_retryable_conflict(exc):
    """提交冲突（catalog 写锁 / 旧快照）才重试，业务错误一律不重试。"""
    if DB_BACKEND != "ducklake" or isinstance(exc, IntegrityError):
        return False
    detail = f"{exc} {getattr(exc, 'orig', '')}"
    return any(marker in detail for marker in _RETRYABLE_COMMIT_MARKERS)


def _commit_with_retry(sess):
    attempts = COMMIT_RETRY_ATTEMPTS if DB_BACKEND == "ducklake" else 1
    if DB_BACKEND == "ducklake" and sess.info.get(_WRITE_FLAG):
        with _commit_lock:
            _commit_loop(sess, attempts)
    else:
        _commit_loop(sess, attempts)


def _commit_loop(sess, attempts):
    delay = COMMIT_RETRY_BASE_DELAY
    retried = False
    for attempt in range(1, attempts + 1):
        try:
            sess.commit()
            if retried:
                _refresh_expired(sess)
            return
        except Exception as exc:
            retryable = attempt < attempts and is_retryable_conflict(exc)
            sess.rollback()
            if not retryable:
                raise
            retried = True
            _replay_flush(sess)
            # 同时失败的事务会一起重试、再撞一次，随机抖动把它们错开。
            time.sleep(delay * (0.5 + random.random()))
            delay = min(delay * COMMIT_RETRY_BACKOFF, COMMIT_RETRY_MAX_DELAY)


def reset_id_counter(table_name, last_id, ducklake_dir=None):
    """把计数器 restart 到「已用掉的最大 id」，下一次 next_id() 就是 last_id + 1。

    迁移搬完历史行（显式带 id）后必须调用，否则新插入从 1 开始、直接撞上搬过来的
    主键——DuckLake 没有 PK 兜底，重号会静默入库（复核实测：库里两条同 id 行都在，
    ORM 因身份映射只回一条）。计数器是 last_id 而不是 last_id+1：next_id() 的语义是
    「先自增再返回」，与 DB 的序列不同。
    """
    conn = _counter_conn(ducklake_dir)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS id_seq (name TEXT PRIMARY KEY, next INTEGER)")
        conn.execute(
            "INSERT INTO id_seq (name, next) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET next = excluded.next",
            (table_name, int(last_id)),
        )
    finally:
        conn.close()


def _assign_app_id(mapper, connection, target):
    # 后端在调用时判（而不是 import 时判）：测试里切后端/"启动后改配置"都能生效，
    # 生产上 LABFLOW_DB 在进程启动时就定了，行为不变。
    if DB_BACKEND != "ducklake" or target.id is not None:
        return
    target.id = _next_id_above(mapper.class_.__tablename__, connection)


def _next_id_above(table_name, connection):
    """取号，并保证比库里已有的 max(id) 大。

    计数器文件被删、被拨回去（复核实测：把计数器调小后新行会拿到已存在的 id）时，
    单看计数器会静默重号——DuckLake 没有主键兜底，两条同 id 行都会入库，ORM 因身份
    映射只回一条。这里在下发前对一次 max(id)，落后就把计数器抬到 max(id) 再取。
    """
    for _ in range(3):
        candidate = next_id(table_name)
        max_id = int(connection.execute(text(f"SELECT max(id) FROM {table_name}")).scalar() or 0)
        if candidate > max_id:
            return candidate
        reset_id_counter(table_name, max_id)
    raise RuntimeError(f"{table_name} 的 id 计数器无法对齐到 max(id)+1（连续落后）")


# 挂在 mapper 上（而不是 handler 的建对象处），所有 ORM 插入都在 flush 前拿到号；
# 非 DuckLake 后端由上面的 _assign_app_id 直接放行，交回数据库自己给 id。
for _model in (User, Project, Batch, FileVersion):
    event.listen(_model, "before_insert", _assign_app_id)


def _shutdown():
    global _engine
    if _engine is not None:
        _engine.dispose()


# 进程退出（含异常路径）时释放引擎，让 DuckDB 的文件锁及时放开。
atexit.register(_shutdown)


def get_engine():
    global _engine
    if _engine is None:
        if DB_BACKEND == "ducklake":
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
        # 挂在这个 sessionmaker 上（而不是全局 Session），只有本进程的库会话会记录重放数据。
        event.listen(_Session, "before_flush", _capture_flush)
        event.listen(_Session, "do_orm_execute", _capture_core_dml)
    return _Session()


@contextmanager
def session():
    s = make_session()
    try:
        yield s
        _commit_with_retry(s)
        s.info.pop(_CLAIMS_KEY, None)
    except Exception:
        s.rollback()
        _undo_claims(s)
        raise
    finally:
        s.info.pop(_REPLAY_KEY, None)
        s.info.pop(_CORE_DML_KEY, None)
        s.close()


def init_db():
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
    ensure_dirs()
    create_all(get_engine())
    migrate_schema()
    resync_id_counters()
    resync_name_claims()
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
