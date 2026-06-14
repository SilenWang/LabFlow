import datetime as dt
import json
import mimetypes
import re
import secrets
import shutil
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote, urlparse, parse_qs

from sqlalchemy import desc as sql_desc, func as sql_func
from sqlalchemy.exc import IntegrityError

from server.auth import read_signed, verify_password, password_hash, sign_payload
from server.config import ROLES, DATE_FIELDS, TEXT_FIELDS, FILE_FIELDS, FILE_LABELS, FILE_EXTENSIONS
from server.config import STATIC_DIR, UPLOAD_DIR, BASE_DIR
from server.db import session as db_session
from server.exceptions import RequestError
from server.models import User, Project, Batch, FileVersion
from server.router import route_api
from server.serializers import (
    serialize_project, serialize_batch,
    serialize_deleted_project, serialize_deleted_batch, get_batch,
)
from server.utils import now_iso, today_token, quote_bytes
from server.validators import safe_filename, assert_date, clean_text


class LabFlowHandler(BaseHTTPRequestHandler):
    server_version = "LabFlow/1.0"

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def do_PATCH(self):
        self.handle_request("PATCH")

    def do_DELETE(self):
        self.handle_request("DELETE")

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def handle_request(self, method):
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/"):
                route_api(self, method, path, parse_qs(parsed.query))
            else:
                self.serve_static(path)
        except RequestError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except IntegrityError as exc:
            message = "数据已存在或违反唯一性要求"
            if "batches.name" in str(exc):
                message = "批次名称已存在，批次名称必须全系统唯一（包括回收站）"
            if "projects.name" in str(exc):
                message = "项目名称已存在"
            self.send_json({"error": message}, 409)
        except Exception as exc:
            self.log_message("ERROR %s", exc)
            self.send_json({"error": "服务器内部错误"}, 500)

    def public_user(self, user):
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "role_label": ROLES[user.role],
        }

    def get_cookie(self, name):
        raw = self.headers.get("Cookie", "")
        for piece in raw.split(";"):
            if "=" in piece:
                key, value = piece.strip().split("=", 1)
                if key == name:
                    return value
        return None

    def require_user(self):
        payload = read_signed(self.get_cookie("labflow_session"))
        if not payload:
            raise RequestError(401, "请先登录")
        with db_session() as s:
            user = s.query(User).filter(
                User.id == payload.get("uid"), User.active == 1
            ).first()
        if not user:
            raise RequestError(401, "账号不可用，请重新登录")
        return user

    def require_manager(self, user):
        if user.role != "manager":
            raise RequestError(403, "只有总负责人可以执行此操作")

    def require_batch_creator(self, user):
        if user.role not in {"manager", "chem"}:
            raise RequestError(403, "只有总负责人或化学部门可以创建批次")

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise RequestError(400, "JSON 格式错误")

    def parse_multipart(self):
        content_type = self.headers.get("Content-Type", "")
        match = re.search(r"boundary=(.+)", content_type)
        if not match:
            raise RequestError(400, "缺少上传边界")
        boundary = match.group(1).strip('"').encode("utf-8")
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 50 * 1024 * 1024:
            raise RequestError(413, "上传文件过大")
        body = self.rfile.read(length)
        parts = {}
        for chunk in body.split(b"--" + boundary):
            if chunk.startswith(b"\r\n"):
                chunk = chunk[2:]
            if chunk.endswith(b"--\r\n"):
                chunk = chunk[:-4]
            elif chunk.endswith(b"--"):
                chunk = chunk[:-2]
            if chunk.endswith(b"\r\n"):
                chunk = chunk[:-2]
            if not chunk:
                continue
            header_blob, sep, content = chunk.partition(b"\r\n\r\n")
            if not sep:
                continue
            headers = header_blob.decode("utf-8", "replace").split("\r\n")
            disposition = next((h for h in headers if h.lower().startswith("content-disposition:")), "")
            name_match = re.search(r'name="([^"]+)"', disposition)
            if not name_match:
                continue
            filename_match = re.search(r'filename="([^"]*)"', disposition)
            name = name_match.group(1)
            if filename_match:
                part = {"filename": filename_match.group(1), "content": content}
            else:
                part = content.decode("utf-8", "replace")
            if name in parts:
                if not isinstance(parts[name], list):
                    parts[name] = [parts[name]]
                parts[name].append(part)
            else:
                parts[name] = part
        return parts

    def me(self, user):
        self.send_json({"user": self.public_user(user), "roles": ROLES})

    def login(self):
        payload = self.read_json()
        username = clean_text(payload.get("username"), 60)
        password = str(payload.get("password") or "")
        with db_session() as s:
            user = s.query(User).filter(
                User.username == username, User.active == 1
            ).first()
        if not user or not verify_password(password, user.password_salt, user.password_hash):
            raise RequestError(401, "账号或密码错误")
        token = sign_payload({
            "uid": user.id,
            "exp": (dt.datetime.now() + dt.timedelta(days=7)).timestamp(),
        })
        body = json.dumps({"user": self.public_user(user)}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", f"labflow_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=604800")
        self.end_headers()
        self.wfile.write(body)

    def logout(self):
        self.send_response(204)
        self.send_header("Set-Cookie", "labflow_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
        self.end_headers()

    def list_users(self, user):
        self.require_manager(user)
        with db_session() as s:
            users = s.query(User).order_by(User.role, User.id).all()
        self.send_json({"users": [{
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "role": u.role,
            "active": u.active,
            "created_at": u.created_at,
        } for u in users], "roles": ROLES})

    def change_password(self, user):
        payload = self.read_json()
        old_password = str(payload.get("old_password") or "")
        new_password = str(payload.get("new_password") or "")
        if len(new_password) < 6:
            raise RequestError(400, "新密码至少 6 位")
        if not verify_password(old_password, user.password_salt, user.password_hash):
            raise RequestError(403, "旧密码错误")
        salt, digest = password_hash(new_password)
        with db_session() as s:
            u = s.get(User, user.id)
            u.password_salt = salt
            u.password_hash = digest
        self.send_json({"ok": True})

    def reset_password(self, user):
        self.require_manager(user)
        payload = self.read_json()
        user_id = int(payload.get("user_id") or 0)
        new_password = str(payload.get("new_password") or "")
        if len(new_password) < 6:
            raise RequestError(400, "新密码至少 6 位")
        salt, digest = password_hash(new_password)
        with db_session() as s:
            u = s.get(User, user_id)
            if not u:
                raise RequestError(404, "用户不存在")
            u.password_salt = salt
            u.password_hash = digest
        self.send_json({"ok": True})

    def list_projects(self):
        with db_session() as s:
            projects = s.query(Project).filter(
                Project.deleted_at.is_(None)
            ).order_by(Project.name).all()
        self.send_json({"projects": [serialize_project(p) for p in projects]})

    def list_trash(self, user):
        self.require_manager(user)
        with db_session() as s:
            batch_count_subq = s.query(
                Batch.project_id,
                sql_func.count(Batch.id).label("cnt"),
            ).filter(Batch.deleted_at.isnot(None)).group_by(Batch.project_id).subquery()

            project_rows = s.query(
                Project.id,
                Project.name,
                Project.created_at,
                Project.deleted_at,
                sql_func.coalesce(batch_count_subq.c.cnt, 0).label("deleted_batch_count"),
            ).outerjoin(
                batch_count_subq, Project.id == batch_count_subq.c.project_id
            ).filter(Project.deleted_at.isnot(None)).order_by(
                sql_desc(Project.deleted_at), sql_desc(Project.id),
            ).all()

            batch_rows = s.query(
                Batch.id,
                Batch.project_id,
                Project.name.label("project_name"),
                Project.deleted_at.label("project_deleted_at"),
                Batch.batch_no,
                Batch.name,
                Batch.remark,
                Batch.synthesis_submitted_date,
                Batch.synthesis_completed_date,
                Batch.bio_test_start_date,
                Batch.bio_test_completed_date,
                Batch.created_at,
                Batch.updated_at,
                Batch.deleted_at,
            ).join(Project, Batch.project_id == Project.id).filter(
                Batch.deleted_at.isnot(None)
            ).order_by(sql_desc(Batch.deleted_at), sql_desc(Batch.id)).all()

            file_rows = s.query(
                FileVersion.id,
                FileVersion.file_type,
                FileVersion.original_name,
                FileVersion.size_bytes,
                FileVersion.uploaded_by,
                FileVersion.uploaded_at,
                FileVersion.deleted_at,
                User.display_name.label("uploaded_by_name"),
                Batch.name.label("batch_name"),
                Batch.batch_no,
                Batch.deleted_at.label("batch_deleted_at"),
                Project.name.label("project_name"),
                Project.deleted_at.label("project_deleted_at"),
            ).join(User, FileVersion.uploaded_by == User.id).join(
                Batch, FileVersion.batch_id == Batch.id
            ).join(Project, Batch.project_id == Project.id).filter(
                FileVersion.deleted_at.isnot(None)
            ).order_by(sql_desc(FileVersion.deleted_at), sql_desc(FileVersion.id)).all()

            files = []
            for row in file_rows:
                files.append({
                    "id": row.id,
                    "file_type": row.file_type,
                    "label": FILE_LABELS.get(row.file_type, row.file_type),
                    "original_name": row.original_name,
                    "size_bytes": row.size_bytes,
                    "uploaded_by": row.uploaded_by_name,
                    "uploaded_at": row.uploaded_at,
                    "deleted_at": row.deleted_at,
                    "batch_name": row.batch_name,
                    "batch_no": row.batch_no,
                    "project_name": row.project_name,
                    "batch_deleted_at": row.batch_deleted_at,
                    "project_deleted_at": row.project_deleted_at,
                })
        self.send_json({
            "projects": [serialize_deleted_project(row) for row in project_rows],
            "batches": [serialize_deleted_batch(s, row) for row in batch_rows],
            "files": files,
        })

    def create_project(self, user):
        self.require_manager(user)
        payload = self.read_json()
        name = clean_text(payload.get("name"), 80)
        if not name:
            raise RequestError(400, "项目名称不能为空")
        with db_session() as s:
            project = Project(name=name, created_by=user.id, created_at=now_iso())
            s.add(project)
            s.flush()
        self.send_json({"project": serialize_project(project)}, 201)

    def update_project(self, user, project_id):
        self.require_manager(user)
        payload = self.read_json()
        name = clean_text(payload.get("name"), 80)
        if not name:
            raise RequestError(400, "项目名称不能为空")
        with db_session() as s:
            project = s.query(Project).filter(
                Project.id == project_id, Project.deleted_at.is_(None)
            ).first()
            if not project:
                raise RequestError(404, "项目不存在")
            project.name = name
        self.send_json({"ok": True})

    def delete_project(self, user, project_id):
        self.require_manager(user)
        with db_session() as s:
            deleted_at = now_iso()
            project = s.query(Project).filter(
                Project.id == project_id, Project.deleted_at.is_(None)
            ).first()
            if not project:
                raise RequestError(404, "项目不存在")
            project.deleted_at = deleted_at
            s.query(Batch).filter(
                Batch.project_id == project_id, Batch.deleted_at.is_(None)
            ).update({Batch.deleted_at: deleted_at})
        self.send_json({"ok": True})

    def restore_project(self, user, project_id):
        self.require_manager(user)
        with db_session() as s:
            project = s.query(Project).filter(
                Project.id == project_id, Project.deleted_at.isnot(None)
            ).first()
            if not project:
                raise RequestError(404, "回收站中没有这个项目")
            project.deleted_at = None
            self.send_json({"project": serialize_project(project)})

    def list_batches(self, query):
        project_id = (query.get("project_id") or [None])[0]
        with db_session() as s:
            q = s.query(Batch).join(Project).filter(
                Batch.deleted_at.is_(None),
                Project.deleted_at.is_(None),
            )
            if project_id and project_id != "all":
                q = q.filter(Batch.project_id == int(project_id))
            q = q.order_by(Project.name, sql_desc(Batch.created_at), sql_desc(Batch.id))
            rows = q.all()
            batches = [serialize_batch(s, row) for row in rows]
        self.send_json({"batches": batches})

    def create_batch(self, user):
        self.require_batch_creator(user)
        payload = self.read_json()
        project_id = int(payload.get("project_id") or 0)
        batch_no = clean_text(payload.get("batch_no"), 80)
        name = clean_text(payload.get("name"), 120)
        remark = clean_text(payload.get("remark"), 1000)
        if not project_id:
            raise RequestError(400, "请选择项目")
        if not batch_no:
            raise RequestError(400, "批次编号不能为空")
        if not name:
            raise RequestError(400, "批次名称不能为空，且必须全系统唯一")
        with db_session() as s:
            project = s.query(Project).filter(
                Project.id == project_id, Project.deleted_at.is_(None)
            ).first()
            if not project:
                raise RequestError(404, "项目不存在")
            new_batch = Batch(
                project_id=project_id,
                batch_no=batch_no,
                name=name,
                remark=remark,
                created_by=user.id,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            s.add(new_batch)
            s.flush()
            row = get_batch(s, new_batch.id)
            batch = serialize_batch(s, row)
        self.send_json({"batch": batch}, 201)

    def update_batch(self, user, batch_id):
        payload = self.read_json()
        allowed = {}
        for field, roles in DATE_FIELDS.items():
            if field in payload:
                if user.role not in roles:
                    raise RequestError(403, f"无权编辑 {field}")
                allowed[field] = assert_date(payload[field])
        for field, roles in TEXT_FIELDS.items():
            if field in payload:
                if user.role not in roles:
                    raise RequestError(403, f"无权编辑 {field}")
                if field == "project_id":
                    allowed[field] = int(payload[field])
                elif field == "remark":
                    allowed[field] = clean_text(payload[field], 1000)
                else:
                    allowed[field] = clean_text(payload[field], 120)
        if "batch_no" in allowed and not allowed["batch_no"]:
            raise RequestError(400, "批次编号不能为空")
        if "name" in allowed and not allowed["name"]:
            raise RequestError(400, "批次名称不能为空，且必须全系统唯一")
        if not allowed:
            raise RequestError(400, "没有可更新的字段")
        allowed["updated_at"] = now_iso()
        with db_session() as s:
            if "project_id" in allowed:
                project = s.query(Project).filter(
                    Project.id == allowed["project_id"], Project.deleted_at.is_(None)
                ).first()
                if not project:
                    raise RequestError(404, "项目不存在")
            batch = s.query(Batch).filter(
                Batch.id == batch_id, Batch.deleted_at.is_(None)
            ).first()
            if not batch:
                raise RequestError(404, "批次不存在")
            for key, value in allowed.items():
                setattr(batch, key, value)
            row = get_batch(s, batch_id)
            batch_data = serialize_batch(s, row)
        self.send_json({"batch": batch_data})

    def delete_batch(self, user, batch_id):
        self.require_manager(user)
        now = now_iso()
        with db_session() as s:
            batch = s.query(Batch).filter(
                Batch.id == batch_id, Batch.deleted_at.is_(None)
            ).first()
            if not batch:
                raise RequestError(404, "批次不存在")
            batch.deleted_at = now
            batch.updated_at = now
        self.send_json({"ok": True})

    def restore_batch(self, user, batch_id):
        self.require_manager(user)
        with db_session() as s:
            row = s.query(Batch).join(Project).filter(Batch.id == batch_id).first()
            if not row or not row.deleted_at:
                raise RequestError(404, "回收站中没有这个批次")
            if row.project.deleted_at:
                raise RequestError(409, "请先恢复该批次所属项目")
            row.deleted_at = None
            row.updated_at = now_iso()
            restored = get_batch(s, batch_id)
            batch = serialize_batch(s, restored)
        self.send_json({"batch": batch})

    def upload_file(self, user, batch_id):
        parts = self.parse_multipart()
        file_type = parts.get("file_type")
        file_part = parts.get("file")
        if file_type not in FILE_FIELDS:
            raise RequestError(400, "文件类型不正确")
        if user.role not in FILE_FIELDS[file_type]:
            raise RequestError(403, "无权上传该类型文件")
        file_parts = file_part if isinstance(file_part, list) else [file_part]
        file_parts = [part for part in file_parts if isinstance(part, dict) and part.get("filename")]
        if not file_parts:
            raise RequestError(400, "请选择文件")
        allowed_extensions = FILE_EXTENSIONS[file_type]
        with db_session() as s:
            batch = get_batch(s, batch_id)
            if not batch:
                raise RequestError(404, "批次不存在")
            folder = UPLOAD_DIR / str(batch_id) / file_type
            folder.mkdir(parents=True, exist_ok=True)
            uploaded_at = now_iso()
            for part in file_parts:
                original_name = safe_filename(part["filename"])
                if not original_name.lower().endswith(allowed_extensions):
                    if file_type in ("compound_info", "data_summary", "experiment_record", "experiment_summary"):
                        raise RequestError(400, "仅支持 Excel、PDF、Word 或 PPT 文件")
                    raise RequestError(400, "仅支持 Excel 或 CSV 文件")
                content = part["content"]
                if len(content) > 10 * 1024 * 1024:
                    raise RequestError(413, "单个文件不能超过 10MB")
                storage_name = f"{today_token()}_{secrets.token_hex(4)}_{original_name}"
                path = folder / storage_name
                path.write_bytes(content)
                rel_path = path.relative_to(BASE_DIR).as_posix()
                fv = FileVersion(
                    batch_id=batch_id,
                    file_type=file_type,
                    original_name=original_name,
                    storage_path=rel_path,
                    size_bytes=len(content),
                    uploaded_by=user.id,
                    uploaded_at=uploaded_at,
                )
                s.add(fv)
            batch.updated_at = now_iso()
            s.flush()
            row = get_batch(s, batch_id)
            serialized = serialize_batch(s, row)
        self.send_json({"batch": serialized}, 201)

    def download_file(self, file_id):
        with db_session() as s:
            row = s.query(FileVersion).join(Batch).join(Project).filter(
                FileVersion.id == file_id,
                FileVersion.deleted_at.is_(None),
                Batch.deleted_at.is_(None),
                Project.deleted_at.is_(None),
            ).first()
        if not row:
            raise RequestError(404, "文件不存在")
        path = (BASE_DIR / row.storage_path).resolve()
        if not str(path).startswith(str(BASE_DIR.resolve())) or not path.exists():
            raise RequestError(404, "文件不存在")
        mime = mimetypes.guess_type(row.original_name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(path.stat().st_size))
        download_name = row.original_name.encode("utf-8")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote_bytes(download_name))
        self.end_headers()
        with path.open("rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def delete_file(self, user, file_id):
        with db_session() as s:
            row = s.query(FileVersion).join(Batch).filter(
                FileVersion.id == file_id,
                FileVersion.deleted_at.is_(None),
                Batch.deleted_at.is_(None),
            ).first()
            if not row:
                raise RequestError(404, "文件不存在或所在批次已删除")
            if user.role != "manager" and user.role not in FILE_FIELDS.get(row.file_type, set()):
                raise RequestError(403, "无权删除此文件")
            row.deleted_at = now_iso()
        self.send_json({"ok": True})

    def restore_file(self, user, file_id):
        self.require_manager(user)
        with db_session() as s:
            row = s.query(FileVersion).join(Batch).filter(
                FileVersion.id == file_id, FileVersion.deleted_at.isnot(None)
            ).first()
            if not row:
                raise RequestError(404, "文件不存在或未被删除")
            if row.batch.deleted_at:
                raise RequestError(409, "请先恢复批次")
            row.deleted_at = None
        self.send_json({"ok": True})

    def serve_static(self, path):
        if path in ("", "/"):
            file_path = STATIC_DIR / "index.html"
        else:
            file_path = (STATIC_DIR / path.lstrip("/")).resolve()
            if not str(file_path).startswith(str(STATIC_DIR.resolve())):
                raise RequestError(403, "禁止访问")
        if not file_path.exists() or not file_path.is_file():
            file_path = STATIC_DIR / "index.html"
        mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with file_path.open("rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def file_config(self):
        accepts = {key: ",".join(ext for ext in exts) for key, exts in FILE_EXTENSIONS.items()}
        self.send_json({
            "file_accepts": accepts,
            "file_labels": FILE_LABELS,
            "file_fields": {key: list(roles) for key, roles in FILE_FIELDS.items()},
        })

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
