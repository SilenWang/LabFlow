import secrets
from contextlib import contextmanager

from sqlalchemy import (
    Column, Integer, String, ForeignKey, MetaData, Table,
    create_engine, event, func, inspect, insert, select, text,
)
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable, DropTable

from server.auth import password_hash
from server.config import DB_PATH
from server.models import Base, User, Project, Batch, FileVersion
from server.utils import ensure_dirs, now_iso

_engine = None
_Session = None


def get_engine():
    global _engine
    if _engine is None:
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
    engine = get_engine()
    inspector = inspect(engine)
    if "batches" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("batches")]
    if "remark" not in columns:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE batches ADD COLUMN remark TEXT"))
            conn.commit()
    unique_constraints = inspector.get_unique_constraints("batches")
    batch_no_uc = any("batch_no" in uc["column_names"] for uc in unique_constraints)
    name_uc = any("name" in uc["column_names"] for uc in unique_constraints)
    if not batch_no_uc and name_uc:
        return
    with session() as s:
        normalize_batch_names(s)
    engine.dispose()
    _recreate_batches_table(engine)


def _recreate_batches_table(engine):
    temp_meta = MetaData()
    batches_new = Table(
        "batches_new", temp_meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("project_id", Integer, ForeignKey("projects.id"), nullable=False),
        Column("batch_no", String, nullable=False),
        Column("name", String, nullable=False, unique=True),
        Column("remark", String, nullable=True),
        Column("synthesis_submitted_date", String, nullable=True),
        Column("synthesis_completed_date", String, nullable=True),
        Column("bio_test_start_date", String, nullable=True),
        Column("bio_test_completed_date", String, nullable=True),
        Column("created_by", Integer, ForeignKey("users.id"), nullable=False),
        Column("created_at", String, nullable=False),
        Column("updated_at", String, nullable=False),
        Column("deleted_at", String, nullable=True),
    )
    old = Batch.__table__
    col_names = [c.name for c in batches_new.columns]

    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        conn.execute(CreateTable(batches_new))
        conn.execute(insert(batches_new).from_select(col_names, select(old)))
        conn.execute(DropTable(old))
        conn.execute(text("ALTER TABLE batches_new RENAME TO batches"))
        conn.execute(text("PRAGMA foreign_keys = ON"))
        conn.commit()


def normalize_batch_names(s):
    batches = s.query(Batch).order_by(Batch.id).all()
    used = set()
    for b in batches:
        base = (b.name or "").strip()
        if not base:
            base = (b.batch_no or "").strip() or f"Batch-{b.id}"
        if len(base) > 110:
            base = base[:110].strip()
        candidate = base
        if candidate in used:
            candidate = f"{base}-{b.id}"
        while candidate in used:
            candidate = f"{base}-{secrets.token_hex(2)}"
        used.add(candidate)
        if candidate != b.name:
            b.name = candidate


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
