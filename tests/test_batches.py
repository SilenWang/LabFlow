import pytest


class TestCreateBatch:
    def test_manager_create_batch(self, server_url, leader_session, project):
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "B001",
            "name": "批次B001",
        })
        assert r.status_code == 201
        data = r.json()
        assert data["batch"]["batch_no"] == "B001"
        assert data["batch"]["project_id"] == project["id"]

    def test_chem_can_create_batch(self, server_url, chem_session, project):
        r = chem_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "C001",
            "name": "化学批次001",
        })
        assert r.status_code == 201

    def test_bio_cannot_create_batch(self, server_url, bio_session, project):
        r = bio_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "BIO001",
            "name": "生物批次001",
        })
        assert r.status_code == 403

    def test_create_batch_no_project(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": 99999,
            "batch_no": "NOPROJ",
            "name": "无项目批次",
        })
        assert r.status_code == 404

    def test_create_batch_duplicate_name(self, server_url, leader_session, project):
        leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "DUP001",
            "name": "唯一名称",
        })
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "DUP002",
            "name": "唯一名称",
        })
        assert r.status_code == 409

    def test_create_batch_duplicate_batch_no(self, server_url, leader_session, project):
        r1 = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "SAME-NO",
            "name": "批次A",
        })
        assert r1.status_code == 201
        r2 = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "SAME-NO",
            "name": "批次B",
        })
        assert r2.status_code == 201


class TestListBatches:
    def test_list_batches(self, server_url, leader_session, project):
        leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "L001",
            "name": "列表测试",
        })
        r = leader_session.get(f"{server_url}/api/batches?project_id=all")
        assert r.status_code == 200
        assert len(r.json()["batches"]) >= 1

    def test_filter_by_project(self, server_url, leader_session, project):
        leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "F001",
            "name": "过滤测试",
        })
        r = leader_session.get(f"{server_url}/api/batches?project_id={project['id']}")
        assert r.status_code == 200
        for b in r.json()["batches"]:
            assert b["project_id"] == project["id"]


class TestUpdateBatch:
    def test_manager_update_text_field(self, server_url, leader_session, batch):
        r = leader_session.patch(f"{server_url}/api/batches/{batch['id']}", json={
            "name": "新批次名称",
            "remark": "新备注",
        })
        assert r.status_code == 200
        assert r.json()["batch"]["name"] == "新批次名称"

    def test_manager_update_date_field(self, server_url, leader_session, batch):
        r = leader_session.patch(f"{server_url}/api/batches/{batch['id']}", json={
            "synthesis_submitted_date": "2024-06-01",
        })
        assert r.status_code == 200

    def test_chem_update_allowed(self, server_url, chem_session, batch):
        r = chem_session.patch(f"{server_url}/api/batches/{batch['id']}", json={
            "batch_no": "CHEM-NO",
            "synthesis_submitted_date": "2024-06-15",
        })
        assert r.status_code == 200
        assert r.json()["batch"]["batch_no"] == "CHEM-NO"

    def test_chem_cannot_update_bio_field(self, server_url, chem_session, batch):
        r = chem_session.patch(f"{server_url}/api/batches/{batch['id']}", json={
            "bio_test_start_date": "2024-07-01",
        })
        assert r.status_code == 403

    def test_bio_update_allowed(self, server_url, bio_session, batch):
        r = bio_session.patch(f"{server_url}/api/batches/{batch['id']}", json={
            "bio_test_start_date": "2024-07-01",
            "bio_test_completed_date": "2024-07-15",
        })
        assert r.status_code == 200

    def test_bio_cannot_update_chem_field(self, server_url, bio_session, batch):
        r = bio_session.patch(f"{server_url}/api/batches/{batch['id']}", json={
            "synthesis_submitted_date": "2024-06-01",
        })
        assert r.status_code == 403

    def test_update_invalid_date(self, server_url, leader_session, batch):
        r = leader_session.patch(f"{server_url}/api/batches/{batch['id']}", json={
            "synthesis_submitted_date": "invalid-date",
        })
        assert r.status_code == 400

    def test_update_deleted_batch(self, server_url, leader_session, batch):
        bid = batch["id"]
        leader_session.delete(f"{server_url}/api/batches/{bid}")
        r = leader_session.patch(f"{server_url}/api/batches/{bid}", json={"name": "改名"})
        assert r.status_code == 404

    def test_batch_status_logic(self, server_url, leader_session, batch):
        bid = batch["id"]
        assert batch.get("synthesis_submitted_date") is None
        r = leader_session.patch(f"{server_url}/api/batches/{bid}", json={
            "synthesis_submitted_date": "2024-06-01",
        })
        assert r.status_code == 200
        r = leader_session.patch(f"{server_url}/api/batches/{bid}", json={
            "synthesis_completed_date": "2024-06-10",
        })
        assert r.status_code == 200
        r = leader_session.patch(f"{server_url}/api/batches/{bid}", json={
            "bio_test_start_date": "2024-06-15",
        })
        assert r.status_code == 200
        r = leader_session.patch(f"{server_url}/api/batches/{bid}", json={
            "bio_test_completed_date": "2024-06-25",
        })
        assert r.status_code == 200


class TestDeleteBatch:
    def test_manager_delete_batch(self, server_url, leader_session, project):
        r = leader_session.post(f"{server_url}/api/batches", json={
            "project_id": project["id"],
            "batch_no": "DEL001",
            "name": "待删除批次",
        })
        bid = r.json()["batch"]["id"]
        r2 = leader_session.delete(f"{server_url}/api/batches/{bid}")
        assert r2.status_code == 200
        assert r2.json()["ok"] is True

    def test_non_manager_cannot_delete(self, server_url, chem_session, batch):
        r = chem_session.delete(f"{server_url}/api/batches/{batch['id']}")
        assert r.status_code == 403

    def test_delete_nonexistent_batch(self, server_url, leader_session):
        r = leader_session.delete(f"{server_url}/api/batches/99999")
        assert r.status_code == 404
