import secrets
from contextlib import contextmanager

from sqlalchemy import create_engine, event, func, inspect, text
from sqlalchemy.orm import sessionmaker

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
    with engine.connect() as conn:
        sql = next(
            (
                row[0]
                for row in conn.execute(text(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'batches'"
                )).fetchall()
            ),
            None,
        )
    if not sql:
        return
    if "batch_no TEXT NOT NULL UNIQUE" not in sql and "name TEXT NOT NULL UNIQUE" in sql:
        return
    with session() as s:
        normalize_batch_names(s)
    engine.dispose()
    _recreate_batches_table(engine)


def _recreate_batches_table(engine):
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        conn.execute(text("""
            CREATE TABLE batches_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                batch_no TEXT NOT NULL,
                name TEXT NOT NULL UNIQUE,
                remark TEXT,
                synthesis_submitted_date TEXT,
                synthesis_completed_date TEXT,
                bio_test_start_date TEXT,
                bio_test_completed_date TEXT,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT,
                FOREIGN KEY(project_id) REFERENCES projects(id),
                FOREIGN KEY(created_by) REFERENCES users(id)
            )
        """))
        conn.execute(text("""
            INSERT INTO batches_new
            (id, project_id, batch_no, name, remark, synthesis_submitted_date, synthesis_completed_date,
             bio_test_start_date, bio_test_completed_date, created_by, created_at, updated_at, deleted_at)
            SELECT id, project_id, batch_no, name, remark, synthesis_submitted_date, synthesis_completed_date,
                   bio_test_start_date, bio_test_completed_date, created_by, created_at, updated_at, deleted_at
            FROM batches
        """))
        conn.execute(text("DROP TABLE batches"))
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
