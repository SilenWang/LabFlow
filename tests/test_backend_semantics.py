"""seekdb 与 sqlite 的语义差异用例：两个后端下行为必须一致。"""

from server.config import FILENAME_MAX_LENGTH, TEXT_MAX_LENGTH


def _upload(server_url, session, batch_id, file_type, filename, content):
    files = {
        "file_type": (None, file_type),
        "file": (filename, content),
    }
    return session.post(f"{server_url}/api/batches/{batch_id}/files", files=files)


class TestTextLength:
    def test_overlong_text_rejected(self, server_url, leader_session, batch):
        # 列长即校验上限：超出时必须在入库前返回 400，否则 seekdb 会抛 1406（500），
        # 而 sqlite 会照收不误。
        r = leader_session.post(f"{server_url}/api/projects", json={
            "name": "x" * (TEXT_MAX_LENGTH["project_name"] + 1),
        })
        assert r.status_code == 400

        r = leader_session.patch(f"{server_url}/api/batches/{batch['id']}", json={
            "batch_no": "y" * (TEXT_MAX_LENGTH["batch_no"] + 1),
        })
        assert r.status_code == 400

        r = leader_session.patch(f"{server_url}/api/batches/{batch['id']}", json={
            "name": "z" * (TEXT_MAX_LENGTH["name"] + 1),
        })
        assert r.status_code == 400

        long_name = "n" * (FILENAME_MAX_LENGTH + 1) + ".xlsx"
        r = _upload(server_url, leader_session, batch["id"], "compound_info", long_name, b"data")
        assert r.status_code == 400

    def test_text_at_limit_accepted(self, server_url, leader_session):
        name = "x" * TEXT_MAX_LENGTH["project_name"]
        r = leader_session.post(f"{server_url}/api/projects", json={"name": name})
        assert r.status_code == 201
        assert r.json()["project"]["name"] == name  # 不能被静默截断


class TestDuplicateNameMessage:
    def test_duplicate_batch_name_message(self, server_url, leader_session, project):
        payload = {"project_id": project["id"], "batch_no": "DUP-A", "name": "重名批次"}
        assert leader_session.post(f"{server_url}/api/batches", json=payload).status_code == 201
        payload["batch_no"] = "DUP-B"
        r = leader_session.post(f"{server_url}/api/batches", json=payload)
        assert r.status_code == 409
        assert r.json()["error"] == "批次名称已存在，批次名称必须全系统唯一（包括回收站）"

    def test_duplicate_project_name_message(self, server_url, leader_session):
        assert leader_session.post(f"{server_url}/api/projects", json={"name": "重名项目"}).status_code == 201
        r = leader_session.post(f"{server_url}/api/projects", json={"name": "重名项目"})
        assert r.status_code == 409
        assert r.json()["error"] == "项目名称已存在"


class TestCaseSensitiveUniqueNames:
    def test_project_names_differing_in_case_coexist(self, server_url, leader_session):
        assert leader_session.post(f"{server_url}/api/projects", json={"name": "Batch-001"}).status_code == 201
        r = leader_session.post(f"{server_url}/api/projects", json={"name": "batch-001"})
        assert r.status_code == 201

    def test_batch_names_differing_in_case_coexist(self, server_url, leader_session, project):
        first = {"project_id": project["id"], "batch_no": "CASE-A", "name": "Batch-001"}
        second = {"project_id": project["id"], "batch_no": "CASE-B", "name": "batch-001"}
        assert leader_session.post(f"{server_url}/api/batches", json=first).status_code == 201
        assert leader_session.post(f"{server_url}/api/batches", json=second).status_code == 201

        # 大小写敏感不等于放弃唯一性：完全同名仍要拦下。
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"], "batch_no": "CASE-C", "name": "Batch-001",
        })
        assert r.status_code == 409
