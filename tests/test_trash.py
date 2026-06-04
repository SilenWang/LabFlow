import pytest


class TestTrash:
    def test_list_trash_empty(self, server_url, leader_session):
        r = leader_session.get(f"{server_url}/api/trash")
        assert r.status_code == 200
        data = r.json()
        assert data["projects"] == []
        assert data["batches"] == []
        assert data["files"] == []

    def test_non_manager_cannot_view_trash(self, server_url, chem_session):
        r = chem_session.get(f"{server_url}/api/trash")
        assert r.status_code == 403

    def test_deleted_project_in_trash(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/projects", json={"name": "待回收项目"})
        pid = r.json()["project"]["id"]
        leader_session.delete(f"{server_url}/api/projects/{pid}")
        r2 = leader_session.get(f"{server_url}/api/trash")
        assert any(p["id"] == pid for p in r2.json()["projects"])

    def test_deleted_batch_in_trash(self, server_url, leader_session, project):
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "TRASH001",
            "name": "待回收批次",
        })
        bid = r.json()["batch"]["id"]
        leader_session.delete(f"{server_url}/api/batches/{bid}")
        r2 = leader_session.get(f"{server_url}/api/trash")
        data = r2.json()
        assert "batches" in data
        assert any(b["id"] == bid for b in data["batches"])

    def test_deleted_file_in_trash(self, server_url, leader_session, batch):
        from tests.test_files import _upload
        r = _upload(server_url, leader_session, batch["id"], "compound_info",
                     "trash.xlsx", b"trash")
        file_id = r.json()["batch"]["files"]["compound_info"]["latest"]["id"]
        leader_session.delete(f"{server_url}/api/files/{file_id}")
        r2 = leader_session.get(f"{server_url}/api/trash")
        assert any(f["id"] == file_id for f in r2.json()["files"])


class TestRestoreProject:
    def test_restore_project(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/projects", json={"name": "恢复项目"})
        pid = r.json()["project"]["id"]
        leader_session.delete(f"{server_url}/api/projects/{pid}")
        r2 = leader_session.post(f"{server_url}/api/projects/{pid}/restore")
        assert r2.status_code == 200
        r3 = leader_session.get(f"{server_url}/api/projects")
        assert any(p["id"] == pid for p in r3.json()["projects"])

    def test_restore_active_project(self, server_url, leader_session, project):
        r = leader_session.post(f"{server_url}/api/projects/{project['id']}/restore")
        assert r.status_code == 404

    def test_non_manager_cannot_restore(self, server_url, project, chem_session):
        pid = project["id"]
        chem_session.delete(f"{server_url}/api/projects/{pid}")
        r = chem_session.post(f"{server_url}/api/projects/{pid}/restore")
        assert r.status_code == 403


class TestRestoreBatch:
    def test_restore_batch(self, server_url, leader_session, project):
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "REST001",
            "name": "待恢复批次",
        })
        bid = r.json()["batch"]["id"]
        leader_session.delete(f"{server_url}/api/batches/{bid}")
        r2 = leader_session.post(f"{server_url}/api/batches/{bid}/restore")
        assert r2.status_code == 200

    def test_restore_batch_when_project_deleted(self, server_url, leader_session, project):
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "REST002",
            "name": "项目已删批次",
        })
        bid = r.json()["batch"]["id"]
        leader_session.delete(f"{server_url}/api/batches/{bid}")
        leader_session.delete(f"{server_url}/api/projects/{project['id']}")
        r2 = leader_session.post(f"{server_url}/api/batches/{bid}/restore")
        assert r2.status_code == 409
        assert "项目" in r2.json()["error"]

    def test_restore_active_batch(self, server_url, leader_session, batch):
        r = leader_session.post(f"{server_url}/api/batches/{batch['id']}/restore")
        assert r.status_code == 404

    def test_non_manager_cannot_restore_batch(self, server_url, chem_session, project):
        r = chem_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "NR001",
            "name": "非管理员恢复",
        })
        bid = r.json()["batch"]["id"]
        chem_session.delete(f"{server_url}/api/batches/{bid}")
        r2 = chem_session.post(f"{server_url}/api/batches/{bid}/restore")
        assert r2.status_code == 403
