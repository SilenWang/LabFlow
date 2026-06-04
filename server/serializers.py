from server.config import FILE_FIELDS, FILE_LABELS


def row_to_dict(row):
    return dict(row) if row else None


def get_batch(conn, batch_id):
    return conn.execute(
        """SELECT b.*, p.name AS project_name
           FROM batches b
           JOIN projects p ON p.id = b.project_id
           WHERE b.id = ? AND b.deleted_at IS NULL AND p.deleted_at IS NULL""",
        (batch_id,),
    ).fetchone()


def serialize_project(row):
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"]}


def latest_files(conn, batch_id):
    rows = conn.execute(
        """SELECT fv.*, u.display_name AS uploaded_by_name
           FROM file_versions fv
           JOIN users u ON u.id = fv.uploaded_by
           WHERE fv.batch_id = ? AND fv.deleted_at IS NULL
           ORDER BY fv.uploaded_at DESC, fv.id DESC""",
        (batch_id,),
    ).fetchall()
    grouped = {key: [] for key in FILE_FIELDS}
    for row in rows:
        item = {
            "id": row["id"],
            "file_type": row["file_type"],
            "label": FILE_LABELS.get(row["file_type"], row["file_type"]),
            "original_name": row["original_name"],
            "size_bytes": row["size_bytes"],
            "uploaded_by": row["uploaded_by_name"],
            "uploaded_at": row["uploaded_at"],
        }
        grouped.setdefault(row["file_type"], []).append(item)
    return {
        key: {"latest": versions[0] if versions else None, "versions": versions}
        for key, versions in grouped.items()
    }


def serialize_batch(conn, row):
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "project_name": row["project_name"],
        "batch_no": row["batch_no"],
        "name": row["name"] or "",
        "synthesis_submitted_date": row["synthesis_submitted_date"],
        "synthesis_completed_date": row["synthesis_completed_date"],
        "bio_test_start_date": row["bio_test_start_date"],
        "bio_test_completed_date": row["bio_test_completed_date"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "files": latest_files(conn, row["id"]),
    }


def serialize_deleted_project(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "deleted_at": row["deleted_at"],
        "deleted_batch_count": row["deleted_batch_count"],
    }


def serialize_deleted_batch(conn, row):
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "project_name": row["project_name"],
        "project_deleted_at": row["project_deleted_at"],
        "batch_no": row["batch_no"],
        "name": row["name"] or "",
        "synthesis_submitted_date": row["synthesis_submitted_date"],
        "synthesis_completed_date": row["synthesis_completed_date"],
        "bio_test_start_date": row["bio_test_start_date"],
        "bio_test_completed_date": row["bio_test_completed_date"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deleted_at": row["deleted_at"],
        "files": latest_files(conn, row["id"]),
    }
