import pytest


class TestLogin:
    def test_valid_login(self, server_url, client):
        r = client.post(f"{server_url}/api/login", json={
            "username": "leader",
            "password": "labflow123",
        })
        assert r.status_code == 200
        data = r.json()
        assert "user" in data
        assert data["user"]["username"] == "leader"
        assert data["user"]["role"] == "manager"
        assert "labflow_session" in r.headers.get("Set-Cookie", "")

    def test_invalid_password(self, server_url, client):
        r = client.post(f"{server_url}/api/login", json={
            "username": "leader",
            "password": "wrong",
        })
        assert r.status_code == 401
        assert "错误" in r.json()["error"]

    def test_nonexistent_user(self, server_url, client):
        r = client.post(f"{server_url}/api/login", json={
            "username": "nobody",
            "password": "anything",
        })
        assert r.status_code == 401

    def test_empty_username(self, server_url, client):
        r = client.post(f"{server_url}/api/login", json={
            "username": "",
            "password": "labflow123",
        })
        assert r.status_code == 401

    def test_chem_login(self, server_url, client):
        r = client.post(f"{server_url}/api/login", json={
            "username": "chem1",
            "password": "chem123",
        })
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "chem"

    def test_bio_login(self, server_url, client):
        r = client.post(f"{server_url}/api/login", json={
            "username": "bio1",
            "password": "bio123",
        })
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "bio"


class TestLogout:
    def test_logout(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/logout")
        assert r.status_code == 204
        r2 = leader_session.get(f"{server_url}/api/me")
        assert r2.status_code == 401


class TestMe:
    def test_me_authenticated(self, server_url, leader_session):
        r = leader_session.get(f"{server_url}/api/me")
        assert r.status_code == 200
        data = r.json()
        assert data["user"]["username"] == "leader"
        assert "roles" in data

    def test_me_unauthenticated(self, server_url, client):
        r = client.get(f"{server_url}/api/me")
        assert r.status_code == 401


class TestChangePassword:
    def test_change_password(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/change-password", json={
            "old_password": "labflow123",
            "new_password": "newpass123",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        leader_session.cookies.clear()
        r = leader_session.post(f"{server_url}/api/login", json={
            "username": "leader",
            "password": "newpass123",
        })
        assert r.status_code == 200

    def test_wrong_old_password(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/change-password", json={
            "old_password": "wrong",
            "new_password": "newpass123",
        })
        assert r.status_code == 403

    def test_short_new_password(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/change-password", json={
            "old_password": "labflow123",
            "new_password": "123",
        })
        assert r.status_code == 400

    def test_non_manager_can_change_own_password(self, server_url, chem_session):
        r = chem_session.post(f"{server_url}/api/change-password", json={
            "old_password": "chem123",
            "new_password": "newchem456",
        })
        assert r.status_code == 200


class TestResetPassword:
    def test_manager_reset_other_user(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/reset-password", json={
            "user_id": 2,
            "new_password": "reset1234",
        })
        assert r.status_code == 200

    def test_non_manager_cannot_reset(self, server_url, chem_session):
        r = chem_session.post(f"{server_url}/api/reset-password", json={
            "user_id": 1,
            "new_password": "test1234",
        })
        assert r.status_code == 403

    def test_reset_nonexistent_user(self, server_url, leader_session):
        r = leader_session.post(f"{server_url}/api/reset-password", json={
            "user_id": 999,
            "new_password": "test1234",
        })
        assert r.status_code == 404
