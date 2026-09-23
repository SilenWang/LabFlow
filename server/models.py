from sqlalchemy import Column, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship

from server.config import TEXT_MAX_LENGTH

Base = declarative_base()


def _unique_text(length):
    # 唯一性文本列在 MySQL/seekdb 下显式 utf8mb4_bin：默认的 utf8mb4_general_ci
    # 大小写不敏感，会把 "Batch-001" 与 "batch-001" 判成重复，偏离 sqlite 语义。
    # sqlite 的默认 BINARY 排序本就是区分大小写的，故只在 mysql 方言上带 collation。
    return String(length).with_variant(String(length, collation="utf8mb4_bin"), "mysql")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(_unique_text(TEXT_MAX_LENGTH["username"]), nullable=False)
    display_name = Column(String(TEXT_MAX_LENGTH["display_name"]), nullable=False)
    role = Column(String(20), nullable=False)
    password_salt = Column(String(64), nullable=False)
    password_hash = Column(String(128), nullable=False)
    active = Column(Integer, nullable=False, default=1)
    created_at = Column(String(32), nullable=False)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("name", name="uq_projects_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(_unique_text(TEXT_MAX_LENGTH["project_name"]), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(String(32), nullable=False)
    deleted_at = Column(String(32), nullable=True)

    creator = relationship("User")


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("name", name="uq_batches_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    batch_no = Column(String(TEXT_MAX_LENGTH["batch_no"]), nullable=False)
    name = Column(_unique_text(TEXT_MAX_LENGTH["name"]), nullable=False)
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

    id = Column(Integer, primary_key=True, autoincrement=True)
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
