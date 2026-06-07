import pytest


class TestCreateProject:
    def test_manager_create_project(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/projects", json={"name": "新建项目"})
        assert r.status_code == 201
        data = r.json()
        assert data["project"]["name"] == "新建项目"
        assert "id" in data["project"]

    def test_create_duplicate_name(self, server_url, leader_session):
        leader_session.post(f"{server_url}/api/projects", json={"name": "重复项目"})
        r = leader_session.post(f"{server_url}/api/projects", json={"name": "重复项目"})
        assert r.status_code == 409
        assert "名称" in r.json()["error"]

    def test_non_manager_cannot_create(self, server_url, chem_session):
        r = chem_session.post(f"{server_url}/api/projects", json={"name": "chem项目"})
        assert r.status_code == 403

    def test_create_empty_name(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/projects", json={"name": ""})
        assert r.status_code == 400

    def test_create_long_name(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/projects", json={"name": "x" * 81})
        assert r.status_code == 400


class TestListProjects:
    def test_list_empty(self, server_url, leader_session):
        r = leader_session.get(f"{server_url}/api/projects")
        assert r.status_code == 200
        assert isinstance(r.json()["projects"], list)

    def test_list_with_projects(self, server_url, leader_session):
        leader_session.post(f"{server_url}/api/projects", json={"name": "项目A"})
        leader_session.post(f"{server_url}/api/projects", json={"name": "项目B"})
        r = leader_session.get(f"{server_url}/api/projects")
        names = [p["name"] for p in r.json()["projects"]]
        assert "项目A" in names
        assert "项目B" in names

    def test_anyone_can_list(self, server_url, chem_session):
        r = chem_session.get(f"{server_url}/api/projects")
        assert r.status_code == 200

    def test_deleted_project_not_listed(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/projects", json={"name": "将被删除"})
        pid = r.json()["project"]["id"]
        leader_session.delete(f"{server_url}/api/projects/{pid}")
        r2 = leader_session.get(f"{server_url}/api/projects")
        names = [p["name"] for p in r2.json()["projects"]]
        assert "将被删除" not in names


class TestUpdateProject:
    def test_rename_project(self, server_url, leader_session, project):
        r = leader_session.patch(f"{server_url}/api/projects/{project['id']}", json={"name": "新名称"})
        assert r.status_code == 200
        r2 = leader_session.get(f"{server_url}/api/projects")
        names = [p["name"] for p in r2.json()["projects"]]
        assert "新名称" in names

    def test_non_manager_cannot_rename(self, server_url, chem_session, project):
        r = chem_session.patch(f"{server_url}/api/projects/{project['id']}", json={"name": "hack"})
        assert r.status_code == 403

    def test_rename_deleted_project(self, server_url, leader_session, project):
        pid = project["id"]
        leader_session.delete(f"{server_url}/api/projects/{pid}")
        r = leader_session.patch(f"{server_url}/api/projects/{pid}", json={"name": "改名"})
        assert r.status_code == 404


class TestDeleteProject:
    def test_manager_delete_project(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/projects", json={"name": "临时项目"})
        pid = r.json()["project"]["id"]
        r2 = leader_session.delete(f"{server_url}/api/projects/{pid}")
        assert r2.status_code == 200
        assert r2.json()["ok"] is True

    def test_non_manager_cannot_delete(self, server_url, project, chem_session):
        r = chem_session.delete(f"{server_url}/api/projects/{project['id']}")
        assert r.status_code == 403

    def test_delete_nonexistent_project(self, server_url, leader_session):
        r = leader_session.delete(f"{server_url}/api/projects/99999")
        assert r.status_code == 404
