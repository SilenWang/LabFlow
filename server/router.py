import re

from server.exceptions import RequestError


def route_api(handler, method, path, query):
    if method == "POST" and path == "/api/login":
        return handler.login()
    if method == "POST" and path == "/api/logout":
        return handler.logout()
    user = handler.require_user()
    if method == "GET" and path == "/api/me":
        return handler.me(user)
    if method == "GET" and path == "/api/file-config":
        return handler.file_config()
    if method == "GET" and path == "/api/users":
        return handler.list_users(user)
    if method == "POST" and path == "/api/change-password":
        return handler.change_password(user)
    if method == "POST" and path == "/api/reset-password":
        return handler.reset_password(user)
    if method == "GET" and path == "/api/trash":
        return handler.list_trash(user)
    if path == "/api/projects":
        if method == "GET":
            return handler.list_projects()
        if method == "POST":
            return handler.create_project(user)
    project_match = re.fullmatch(r"/api/projects/(\d+)", path)
    if project_match:
        project_id = int(project_match.group(1))
        if method == "PATCH":
            return handler.update_project(user, project_id)
        if method == "DELETE":
            return handler.delete_project(user, project_id)
    project_restore_match = re.fullmatch(r"/api/projects/(\d+)/restore", path)
    if project_restore_match and method == "POST":
        return handler.restore_project(user, int(project_restore_match.group(1)))
    if path == "/api/batches":
        if method == "GET":
            return handler.list_batches(query)
        if method == "POST":
            return handler.create_batch(user)
    batch_match = re.fullmatch(r"/api/batches/(\d+)", path)
    if batch_match:
        batch_id = int(batch_match.group(1))
        if method == "PATCH":
            return handler.update_batch(user, batch_id)
        if method == "DELETE":
            return handler.delete_batch(user, batch_id)
    batch_restore_match = re.fullmatch(r"/api/batches/(\d+)/restore", path)
    if batch_restore_match and method == "POST":
        return handler.restore_batch(user, int(batch_restore_match.group(1)))
    file_upload_match = re.fullmatch(r"/api/batches/(\d+)/files", path)
    if file_upload_match and method == "POST":
        return handler.upload_file(user, int(file_upload_match.group(1)))
    file_download_match = re.fullmatch(r"/api/files/(\d+)/download", path)
    if file_download_match and method == "GET":
        return handler.download_file(int(file_download_match.group(1)))
    file_match = re.fullmatch(r"/api/files/(\d+)", path)
    if file_match and method == "DELETE":
        return handler.delete_file(user, int(file_match.group(1)))
    file_restore_match = re.fullmatch(r"/api/files/(\d+)/restore", path)
    if file_restore_match and method == "POST":
        return handler.restore_file(user, int(file_restore_match.group(1)))
    raise RequestError(404, "接口不存在")
