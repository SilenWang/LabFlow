from sqlalchemy import Column, Integer, String, Text, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False, unique=True)
    display_name = Column(String, nullable=False)
    role = Column(String, nullable=False)
    password_salt = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    active = Column(Integer, nullable=False, default=1)
    created_at = Column(String, nullable=False)


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(String, nullable=False)
    deleted_at = Column(String, nullable=True)

    creator = relationship("User")


class Batch(Base):
    __tablename__ = "batches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    batch_no = Column(String, nullable=False)
    name = Column(String, nullable=False, unique=True)
    remark = Column(String, nullable=True)
    synthesis_submitted_date = Column(String, nullable=True)
    synthesis_completed_date = Column(String, nullable=True)
    bio_test_start_date = Column(String, nullable=True)
    bio_test_completed_date = Column(String, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)
    deleted_at = Column(String, nullable=True)

    project = relationship("Project")
    creator = relationship("User")
    files = relationship("FileVersion", back_populates="batch")


class FileVersion(Base):
    __tablename__ = "file_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(Integer, ForeignKey("batches.id"), nullable=False)
    file_type = Column(String, nullable=False)
    original_name = Column(String, nullable=False)
    storage_path = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(String, nullable=False)
    deleted_at = Column(String, nullable=True)

    batch = relationship("Batch", back_populates="files")
    uploader = relationship("User")
