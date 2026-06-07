import pytest


class TestUserList:
    def test_manager_can_list_users(self, server_url, leader_session):
        r = leader_session.get(f"{server_url}/api/users")
        assert r.status_code == 200
        data = r.json()
        assert "users" in data
        assert len(data["users"]) >= 1

    def test_non_manager_cannot_list_users(self, server_url, chem_session):
        r = chem_session.get(f"{server_url}/api/users")
        assert r.status_code == 403

    def test_bio_cannot_list_users(self, server_url, bio_session):
        r = bio_session.get(f"{server_url}/api/users")
        assert r.status_code == 403


class TestAuthRequired:
    def test_all_api_endpoints_require_auth(self, server_url, client):
        endpoints = [
            ("GET", "/api/users"),
            ("GET", "/api/me"),
            ("GET", "/api/projects"),
            ("POST", "/api/projects"),
            ("GET", "/api/trash"),
            ("PATCH", "/api/batches/1"),
            ("DELETE", "/api/batches/1"),
        ]
        for method, path in endpoints:
            r = client.request(method, f"{server_url}{path}", json={})
            assert r.status_code == 401, f"{method} {path} returned {r.status_code}"


class TestViewAccess:
    def test_everyone_can_see_projects(self, server_url):
        import requests
        s = requests.Session()
        s.post(f"{server_url}/api/login", json={
            "username": "bio3",
            "password": "bio123",
        })
        r = s.get(f"{server_url}/api/projects")
        assert r.status_code == 200
        r2 = s.get(f"{server_url}/api/batches?project_id=all")
        assert r2.status_code == 200
        r3 = s.get(f"{server_url}/api/me")
        assert r3.status_code == 200


class TestRoleLabels:
    def test_roles_in_me(self, server_url, leader_session):
        r = leader_session.get(f"{server_url}/api/me")
        data = r.json()
        assert data["roles"] == {
            "manager": "总负责人",
            "chem": "化学部门",
            "bio": "生物部门",
        }

    def test_roles_in_users(self, server_url, leader_session):
        r = leader_session.get(f"{server_url}/api/users")
        data = r.json()
        assert data["roles"] == {
            "manager": "总负责人",
            "chem": "化学部门",
            "bio": "生物部门",
        }


class TestUnauthorizedRoutes:
    def test_invalid_route(self, server_url, leader_session):
        r = leader_session.get(f"{server_url}/api/nonexistent")
        assert r.status_code == 404

    def test_static_file(self, server_url, leader_session):
        r = leader_session.get(f"{server_url}/styles.css")
        assert r.status_code == 200

    def test_static_path_traversal(self, server_url, leader_session):
        r = leader_session.get(f"{server_url}/..%2fserver.py")
        assert r.status_code == 403


class TestExperimentRecordPermissions:
    def test_chem_upload_experiment_record(self, server_url, chem_session, batch):
        from tests.test_files import _upload
        r = _upload(server_url, chem_session, batch["id"], "experiment_record",
                     "record_chem.xlsx", b"record")
        assert r.status_code == 201

    def test_bio_upload_experiment_record(self, server_url, bio_session, batch):
        from tests.test_files import _upload
        r = _upload(server_url, bio_session, batch["id"], "experiment_record",
                     "record_bio.xlsx", b"record")
        assert r.status_code == 201

    def test_chem_upload_experiment_summary(self, server_url, chem_session, batch):
        from tests.test_files import _upload
        r = _upload(server_url, chem_session, batch["id"], "experiment_summary",
                     "summary_chem.xlsx", b"summary")
        assert r.status_code == 201

    def test_bio_upload_experiment_summary(self, server_url, bio_session, batch):
        from tests.test_files import _upload
        r = _upload(server_url, bio_session, batch["id"], "experiment_summary",
                     "summary_bio.xlsx", b"summary")
        assert r.status_code == 201
