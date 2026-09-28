from sqlalchemy import Column, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship

from server.config import DB_BACKEND, TEXT_MAX_LENGTH

Base = declarative_base()


def _pk(table):
    # 自增主键没有两后端通用的写法：sqlite 的 INTEGER PRIMARY KEY 由 autoincrement=True
    # 表达；DuckLake 则没有——它不支持 sequence（连
    # CREATE SEQUENCE 都拒），PK/UNIQUE/FK 也建不了，所以 id 只能交给应用层分配
    # （db.py 的 before_insert + 辅助 SQLite 计数器）。实测见 docs/DL0-DuckLake模型层核实.md。
    if DB_BACKEND == "ducklake":
        return Column(Integer, primary_key=True, autoincrement=False)
    return Column(Integer, primary_key=True, autoincrement=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    id = _pk("users")
    username = Column(String(TEXT_MAX_LENGTH["username"]), nullable=False)
    display_name = Column(String(TEXT_MAX_LENGTH["display_name"]), nullable=False)
    role = Column(String(20), nullable=False)
    password_salt = Column(String(64), nullable=False)
    password_hash = Column(String(128), nullable=False)
    active = Column(Integer, nullable=False, default=1)
    created_at = Column(String(32), nullable=False)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("name", name="uq_projects_name"),)

    id = _pk("projects")
    name = Column(String(TEXT_MAX_LENGTH["project_name"]), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(String(32), nullable=False)
    deleted_at = Column(String(32), nullable=True)

    creator = relationship("User")


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("name", name="uq_batches_name"),)

    id = _pk("batches")
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    batch_no = Column(String(TEXT_MAX_LENGTH["batch_no"]), nullable=False)
    name = Column(String(TEXT_MAX_LENGTH["name"]), nullable=False)
    remark = Column(String(TEXT_MAX_LENGTH["remark"]), nullable=True)
    synthesis_submitted_date = Column(String(10), nullable=True)
    synthesis_completed_date = Column(String(10), nullable=True)
    bio_test_start_date = Column(String(10), nullable=True)
    bio_test_completed_date = Column(String(10), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(String(32), nullable=False)
    updated_at = Column(String(32), nullable=False)
    deleted_at = Column(String(32), nullable=True)

    project = relationship("Project")
    creator = relationship("User")
    files = relationship("FileVersion", back_populates="batch")


class FileVersion(Base):
    __tablename__ = "file_versions"

    id = _pk("file_versions")
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    file_type = Column(String(32), nullable=False)
    original_name = Column(String(255), nullable=False)
    storage_path = Column(String(512), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(String(32), nullable=False)
    deleted_at = Column(String(32), nullable=True)

    batch = relationship("Batch", back_populates="files")
    uploader = relationship("User")
