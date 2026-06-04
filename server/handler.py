import datetime as dt
import json
import mimetypes
import re
import secrets
import shutil
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote, urlparse, parse_qs

from server.auth import read_signed, verify_password, password_hash, sign_payload
from server.config import ROLES, DATE_FIELDS, TEXT_FIELDS, FILE_FIELDS, FILE_LABELS, FILE_EXTENSIONS
from server.config import STATIC_DIR, UPLOAD_DIR, BASE_DIR
from server.db import db
from server.exceptions import RequestError
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
        except sqlite3.IntegrityError as exc:
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
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
            "role_label": ROLES[user["role"]],
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
        with db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE id = ? AND active = 1",
                (payload.get("uid"),),
            ).fetchone()
        if not user:
            raise RequestError(401, "账号不可用，请重新登录")
        return user

    def require_manager(self, user):
        if user["role"] != "manager":
            raise RequestError(403, "只有总负责人可以执行此操作")

    def require_batch_creator(self, user):
        if user["role"] not in {"manager", "chem"}:
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
        with db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE username = ? AND active = 1", (username,)
            ).fetchone()
        if not user or not verify_password(password, user["password_salt"], user["password_hash"]):
            raise RequestError(401, "账号或密码错误")
        token = sign_payload({
            "uid": user["id"],
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
        with db() as conn:
            rows = conn.execute(
                "SELECT id, username, display_name, role, active, created_at FROM users ORDER BY role, id"
            ).fetchall()
        self.send_json({"users": [dict(row) for row in rows], "roles": ROLES})

    def change_password(self, user):
        payload = self.read_json()
        old_password = str(payload.get("old_password") or "")
        new_password = str(payload.get("new_password") or "")
        if len(new_password) < 6:
            raise RequestError(400, "新密码至少 6 位")
        if not verify_password(old_password, user["password_salt"], user["password_hash"]):
            raise RequestError(403, "旧密码错误")
        salt, digest = password_hash(new_password)
        with db() as conn:
            conn.execute(
                "UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
                (salt, digest, user["id"]),
            )
        self.send_json({"ok": True})

    def reset_password(self, user):
        self.require_manager(user)
        payload = self.read_json()
        user_id = int(payload.get("user_id") or 0)
        new_password = str(payload.get("new_password") or "")
        if len(new_password) < 6:
            raise RequestError(400, "新密码至少 6 位")
        salt, digest = password_hash(new_password)
        with db() as conn:
            cur = conn.execute(
                "UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
                (salt, digest, user_id),
            )
            if cur.rowcount == 0:
                raise RequestError(404, "用户不存在")
        self.send_json({"ok": True})

    def list_projects(self):
        with db() as conn:
            rows = conn.execute(
                "SELECT id, name, created_at FROM projects WHERE deleted_at IS NULL ORDER BY name"
            ).fetchall()
        self.send_json({"projects": [serialize_project(row) for row in rows]})

    def list_trash(self, user):
        self.require_manager(user)
        with db() as conn:
            project_rows = conn.execute("""
                SELECT p.id, p.name, p.created_at, p.deleted_at,
                       COUNT(b.id) AS deleted_batch_count
                FROM projects p
                LEFT JOIN batches b ON b.project_id = p.id AND b.deleted_at IS NOT NULL
                WHERE p.deleted_at IS NOT NULL
                GROUP BY p.id
                ORDER BY p.deleted_at DESC, p.id DESC
            """).fetchall()
            batch_rows = conn.execute("""
                SELECT b.id, b.project_id, p.name AS project_name, p.deleted_at AS project_deleted_at,
                       b.batch_no, b.name,
                       b.synthesis_submitted_date, b.synthesis_completed_date,
                       b.bio_test_start_date, b.bio_test_completed_date,
                       b.created_at, b.updated_at, b.deleted_at
                FROM batches b
                JOIN projects p ON p.id = b.project_id
                WHERE b.deleted_at IS NOT NULL
                ORDER BY b.deleted_at DESC, b.id DESC
            """).fetchall()
            file_rows = conn.execute("""
                SELECT fv.*, u.display_name AS uploaded_by_name,
                       b.name AS batch_name, b.batch_no,
                       b.deleted_at AS batch_deleted_at,
                       p.name AS project_name, p.deleted_at AS project_deleted_at
                FROM file_versions fv
                JOIN users u ON u.id = fv.uploaded_by
                JOIN batches b ON b.id = fv.batch_id
                JOIN projects p ON p.id = b.project_id
                WHERE fv.deleted_at IS NOT NULL
                ORDER BY fv.deleted_at DESC, fv.id DESC
            """).fetchall()
            files = []
            for row in file_rows:
                files.append({
                    "id": row["id"],
                    "file_type": row["file_type"],
                    "label": FILE_LABELS.get(row["file_type"], row["file_type"]),
                    "original_name": row["original_name"],
                    "size_bytes": row["size_bytes"],
                    "uploaded_by": row["uploaded_by_name"],
                    "uploaded_at": row["uploaded_at"],
                    "deleted_at": row["deleted_at"],
                    "batch_name": row["batch_name"],
                    "batch_no": row["batch_no"],
                    "project_name": row["project_name"],
                    "batch_deleted_at": row["batch_deleted_at"],
                    "project_deleted_at": row["project_deleted_at"],
                })
        self.send_json({
            "projects": [serialize_deleted_project(row) for row in project_rows],
            "batches": [serialize_deleted_batch(conn, row) for row in batch_rows],
            "files": files,
        })

    def create_project(self, user):
        self.require_manager(user)
        payload = self.read_json()
        name = clean_text(payload.get("name"), 80)
        if not name:
            raise RequestError(400, "项目名称不能为空")
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO projects (name, created_by, created_at) VALUES (?, ?, ?)",
                (name, user["id"], now_iso()),
            )
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()
        self.send_json({"project": serialize_project(row)}, 201)

    def update_project(self, user, project_id):
        self.require_manager(user)
        payload = self.read_json()
        name = clean_text(payload.get("name"), 80)
        if not name:
            raise RequestError(400, "项目名称不能为空")
        with db() as conn:
            cur = conn.execute(
                "UPDATE projects SET name = ? WHERE id = ? AND deleted_at IS NULL",
                (name, project_id),
            )
            if cur.rowcount == 0:
                raise RequestError(404, "项目不存在")
        self.send_json({"ok": True})

    def delete_project(self, user, project_id):
        self.require_manager(user)
        with db() as conn:
            deleted_at = now_iso()
            cur = conn.execute(
                "UPDATE projects SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (deleted_at, project_id),
            )
            if cur.rowcount == 0:
                raise RequestError(404, "项目不存在")
            conn.execute(
                "UPDATE batches SET deleted_at = ? WHERE project_id = ? AND deleted_at IS NULL",
                (deleted_at, project_id),
            )
        self.send_json({"ok": True})

    def restore_project(self, user, project_id):
        self.require_manager(user)
        with db() as conn:
            cur = conn.execute(
                "UPDATE projects SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
                (project_id,),
            )
            if cur.rowcount == 0:
                raise RequestError(404, "回收站中没有这个项目")
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        self.send_json({"project": serialize_project(row)})

    def list_batches(self, query):
        project_id = (query.get("project_id") or [None])[0]
        sql = """SELECT b.*, p.name AS project_name
                 FROM batches b
                 JOIN projects p ON p.id = b.project_id
                 WHERE b.deleted_at IS NULL AND p.deleted_at IS NULL"""
        params = []
        if project_id and project_id != "all":
            sql += " AND b.project_id = ?"
            params.append(int(project_id))
        sql += " ORDER BY p.name, b.created_at DESC, b.id DESC"
        with db() as conn:
            rows = conn.execute(sql, params).fetchall()
            batches = [serialize_batch(conn, row) for row in rows]
        self.send_json({"batches": batches})

    def create_batch(self, user):
        self.require_batch_creator(user)
        payload = self.read_json()
        project_id = int(payload.get("project_id") or 0)
        batch_no = clean_text(payload.get("batch_no"), 80)
        name = clean_text(payload.get("name"), 120)
        if not project_id:
            raise RequestError(400, "请选择项目")
        if not batch_no:
            raise RequestError(400, "批次编号不能为空")
        if not name:
            raise RequestError(400, "批次名称不能为空，且必须全系统唯一")
        with db() as conn:
            project = conn.execute(
                "SELECT id FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,)
            ).fetchone()
            if not project:
                raise RequestError(404, "项目不存在")
            cur = conn.execute(
                """INSERT INTO batches
                   (project_id, batch_no, name, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (project_id, batch_no, name, user["id"], now_iso(), now_iso()),
            )
            row = get_batch(conn, cur.lastrowid)
            batch = serialize_batch(conn, row)
        self.send_json({"batch": batch}, 201)

    def update_batch(self, user, batch_id):
        payload = self.read_json()
        allowed = {}
        for field, roles in DATE_FIELDS.items():
            if field in payload:
                if user["role"] not in roles:
                    raise RequestError(403, f"无权编辑 {field}")
                allowed[field] = assert_date(payload[field])
        for field, roles in TEXT_FIELDS.items():
            if field in payload:
                if user["role"] not in roles:
                    raise RequestError(403, f"无权编辑 {field}")
                if field == "project_id":
                    allowed[field] = int(payload[field])
                else:
                    allowed[field] = clean_text(payload[field], 120)
        if "batch_no" in allowed and not allowed["batch_no"]:
            raise RequestError(400, "批次编号不能为空")
        if "name" in allowed and not allowed["name"]:
            raise RequestError(400, "批次名称不能为空，且必须全系统唯一")
        if not allowed:
            raise RequestError(400, "没有可更新的字段")
        allowed["updated_at"] = now_iso()
        columns = ", ".join([f"{key} = ?" for key in allowed])
        params = list(allowed.values()) + [batch_id]
        with db() as conn:
            if "project_id" in allowed:
                project = conn.execute(
                    "SELECT id FROM projects WHERE id = ? AND deleted_at IS NULL",
                    (allowed["project_id"],),
                ).fetchone()
                if not project:
                    raise RequestError(404, "项目不存在")
            cur = conn.execute(
                f"UPDATE batches SET {columns} WHERE id = ? AND deleted_at IS NULL",
                params,
            )
            if cur.rowcount == 0:
                raise RequestError(404, "批次不存在")
            row = get_batch(conn, batch_id)
            batch = serialize_batch(conn, row)
        self.send_json({"batch": batch})

    def delete_batch(self, user, batch_id):
        self.require_manager(user)
        with db() as conn:
            cur = conn.execute(
                "UPDATE batches SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now_iso(), now_iso(), batch_id),
            )
            if cur.rowcount == 0:
                raise RequestError(404, "批次不存在")
        self.send_json({"ok": True})

    def restore_batch(self, user, batch_id):
        self.require_manager(user)
        with db() as conn:
            row = conn.execute("""
                SELECT b.*, p.name AS project_name, p.deleted_at AS project_deleted_at
                FROM batches b
                JOIN projects p ON p.id = b.project_id
                WHERE b.id = ?
            """, (batch_id,)).fetchone()
            if not row or not row["deleted_at"]:
                raise RequestError(404, "回收站中没有这个批次")
            if row["project_deleted_at"]:
                raise RequestError(409, "请先恢复该批次所属项目")
            conn.execute(
                "UPDATE batches SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                (now_iso(), batch_id),
            )
            restored = get_batch(conn, batch_id)
            batch = serialize_batch(conn, restored)
        self.send_json({"batch": batch})

    def upload_file(self, user, batch_id):
        parts = self.parse_multipart()
        file_type = parts.get("file_type")
        file_part = parts.get("file")
        if file_type not in FILE_FIELDS:
            raise RequestError(400, "文件类型不正确")
        if user["role"] not in FILE_FIELDS[file_type]:
            raise RequestError(403, "无权上传该类型文件")
        file_parts = file_part if isinstance(file_part, list) else [file_part]
        file_parts = [part for part in file_parts if isinstance(part, dict) and part.get("filename")]
        if not file_parts:
            raise RequestError(400, "请选择文件")
        allowed_extensions = FILE_EXTENSIONS[file_type]
        with db() as conn:
            batch = get_batch(conn, batch_id)
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
                conn.execute(
                    """INSERT INTO file_versions
                       (batch_id, file_type, original_name, storage_path, size_bytes, uploaded_by, uploaded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (batch_id, file_type, original_name, rel_path, len(content), user["id"], uploaded_at),
                )
            conn.execute("UPDATE batches SET updated_at = ? WHERE id = ?", (now_iso(), batch_id))
            row = get_batch(conn, batch_id)
            serialized = serialize_batch(conn, row)
        self.send_json({"batch": serialized}, 201)

    def download_file(self, file_id):
        with db() as conn:
            row = conn.execute("""
                SELECT fv.*
                FROM file_versions fv
                JOIN batches b ON b.id = fv.batch_id
                JOIN projects p ON p.id = b.project_id
                WHERE fv.id = ? AND fv.deleted_at IS NULL
                  AND b.deleted_at IS NULL AND p.deleted_at IS NULL
            """, (file_id,)).fetchone()
        if not row:
            raise RequestError(404, "文件不存在")
        path = (BASE_DIR / row["storage_path"]).resolve()
        if not str(path).startswith(str(BASE_DIR.resolve())) or not path.exists():
            raise RequestError(404, "文件不存在")
        mime = mimetypes.guess_type(row["original_name"])[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(path.stat().st_size))
        download_name = row["original_name"].encode("utf-8")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote_bytes(download_name))
        self.end_headers()
        with path.open("rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def delete_file(self, user, file_id):
        with db() as conn:
            row = conn.execute("""
                SELECT fv.*, b.deleted_at AS batch_deleted_at
                FROM file_versions fv
                JOIN batches b ON b.id = fv.batch_id
                WHERE fv.id = ? AND fv.deleted_at IS NULL
                  AND b.deleted_at IS NULL
            """, (file_id,)).fetchone()
            if not row:
                raise RequestError(404, "文件不存在或所在批次已删除")
            if user["role"] != "manager" and user["role"] not in FILE_FIELDS.get(row["file_type"], set()):
                raise RequestError(403, "无权删除此文件")
            conn.execute(
                "UPDATE file_versions SET deleted_at = ? WHERE id = ?",
                (now_iso(), file_id),
            )
        self.send_json({"ok": True})

    def restore_file(self, user, file_id):
        self.require_manager(user)
        with db() as conn:
            row = conn.execute("""
                SELECT fv.*, b.deleted_at AS batch_deleted_at
                FROM file_versions fv
                JOIN batches b ON b.id = fv.batch_id
                WHERE fv.id = ? AND fv.deleted_at IS NOT NULL
            """, (file_id,)).fetchone()
            if not row:
                raise RequestError(404, "文件不存在或未被删除")
            if row["batch_deleted_at"]:
                raise RequestError(409, "请先恢复批次")
            conn.execute(
                "UPDATE file_versions SET deleted_at = NULL WHERE id = ?",
                (file_id,),
            )
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

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
