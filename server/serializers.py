from sqlalchemy import desc

from server.config import FILE_FIELDS, FILE_LABELS
from server.models import Batch, FileVersion, Project, User


def get_batch(session, batch_id):
    return session.query(Batch).join(Project).filter(
        Batch.id == batch_id,
        Batch.deleted_at.is_(None),
        Project.deleted_at.is_(None),
    ).first()


def serialize_project(project):
    return {"id": project.id, "name": project.name, "created_at": project.created_at}


def latest_files(session, batch_id):
    rows = (
        session.query(FileVersion)
        .join(User)
        .filter(FileVersion.batch_id == batch_id, FileVersion.deleted_at.is_(None))
        .order_by(desc(FileVersion.uploaded_at), desc(FileVersion.id))
        .all()
    )
    grouped = {key: [] for key in FILE_FIELDS}
    for row in rows:
        item = {
            "id": row.id,
            "file_type": row.file_type,
            "label": FILE_LABELS.get(row.file_type, row.file_type),
            "original_name": row.original_name,
            "size_bytes": row.size_bytes,
            "uploaded_by": row.uploader.display_name,
            "uploaded_at": row.uploaded_at,
        }
        grouped.setdefault(row.file_type, []).append(item)
    return {
        key: {"latest": versions[0] if versions else None, "versions": versions}
        for key, versions in grouped.items()
    }


def serialize_batch(session, batch):
    return {
        "id": batch.id,
        "project_id": batch.project_id,
        "project_name": batch.project.name,
        "batch_no": batch.batch_no,
        "name": batch.name or "",
        "remark": batch.remark or "",
        "synthesis_submitted_date": batch.synthesis_submitted_date,
        "synthesis_completed_date": batch.synthesis_completed_date,
        "bio_test_start_date": batch.bio_test_start_date,
        "bio_test_completed_date": batch.bio_test_completed_date,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "files": latest_files(session, batch.id),
    }


def serialize_deleted_project(row):
    return {
        "id": row.id,
        "name": row.name,
        "created_at": row.created_at,
        "deleted_at": row.deleted_at,
        "deleted_batch_count": row.deleted_batch_count,
    }


def serialize_deleted_batch(session, row):
    return {
        "id": row.id,
        "project_id": row.project_id,
        "project_name": row.project_name,
        "project_deleted_at": row.project_deleted_at,
        "batch_no": row.batch_no,
        "name": row.name or "",
        "remark": row.remark or "",
        "synthesis_submitted_date": row.synthesis_submitted_date,
        "synthesis_completed_date": row.synthesis_completed_date,
        "bio_test_start_date": row.bio_test_start_date,
        "bio_test_completed_date": row.bio_test_completed_date,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "deleted_at": row.deleted_at,
        "files": latest_files(session, row.id),
    }
