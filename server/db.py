import secrets
import sqlite3

from server.auth import password_hash
from server.config import DB_PATH
from server.utils import ensure_dirs, now_iso


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    ensure_dirs()
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT,
                FOREIGN KEY(created_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                batch_no TEXT NOT NULL,
                name TEXT NOT NULL UNIQUE,
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
            );

            CREATE TABLE IF NOT EXISTS file_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                file_type TEXT NOT NULL,
                original_name TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                uploaded_by INTEGER NOT NULL,
                uploaded_at TEXT NOT NULL,
                deleted_at TEXT,
                FOREIGN KEY(batch_id) REFERENCES batches(id),
                FOREIGN KEY(uploaded_by) REFERENCES users(id)
            );
        """)
        migrate_schema(conn)
        count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if count == 0:
            seed_users(conn)


def migrate_schema(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'batches'"
    ).fetchone()
    if not row or not row["sql"]:
        return
    sql = row["sql"]
    if "batch_no TEXT NOT NULL UNIQUE" not in sql and "name TEXT NOT NULL UNIQUE" in sql:
        return
    normalize_batch_names(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript("""
        CREATE TABLE batches_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            batch_no TEXT NOT NULL,
            name TEXT NOT NULL UNIQUE,
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
        );

        INSERT INTO batches_new
        (id, project_id, batch_no, name, synthesis_submitted_date, synthesis_completed_date,
         bio_test_start_date, bio_test_completed_date, created_by, created_at, updated_at, deleted_at)
        SELECT id, project_id, batch_no, name, synthesis_submitted_date, synthesis_completed_date,
               bio_test_start_date, bio_test_completed_date, created_by, created_at, updated_at, deleted_at
        FROM batches;

        DROP TABLE batches;
        ALTER TABLE batches_new RENAME TO batches;
    """)
    conn.execute("PRAGMA foreign_keys = ON")


def normalize_batch_names(conn):
    rows = conn.execute("SELECT id, batch_no, name FROM batches ORDER BY id").fetchall()
    used = set()
    for row in rows:
        base = (row["name"] or "").strip()
        if not base:
            base = (row["batch_no"] or "").strip() or f"Batch-{row['id']}"
        if len(base) > 110:
            base = base[:110].strip()
        candidate = base
        if candidate in used:
            candidate = f"{base}-{row['id']}"
        while candidate in used:
            candidate = f"{base}-{secrets.token_hex(2)}"
        used.add(candidate)
        if candidate != row["name"]:
            conn.execute("UPDATE batches SET name = ? WHERE id = ?", (candidate, row["id"]))


def seed_users(conn):
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
        conn.execute(
            """INSERT INTO users (username, display_name, role, password_salt, password_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (username, display_name, role, salt, digest, now_iso()),
        )
