import pytest


def _upload(server_url, session, batch_id, file_type, filename, content):
    files = {
        "file_type": (None, file_type),
        "file": (filename, content),
    }
    return session.post(f"{server_url}/api/batches/{batch_id}/files", files=files)


class TestUploadFile:
    def test_manager_upload_compound_info(self, server_url, leader_session, batch):
        r = _upload(server_url, leader_session, batch["id"], "compound_info",
                     "test.xlsx", b"test content")
        assert r.status_code == 201
        data = r.json()
        assert data["batch"]["files"]["compound_info"]["latest"] is not None
        assert data["batch"]["files"]["compound_info"]["latest"]["original_name"] == "test.xlsx"

    def test_chem_upload_compound_info(self, server_url, chem_session, batch):
        r = _upload(server_url, chem_session, batch["id"], "compound_info",
                     "chem.xlsx", b"chem data")
        assert r.status_code == 201

    def test_bio_upload_bio_raw_data(self, server_url, bio_session, batch):
        r = _upload(server_url, bio_session, batch["id"], "bio_raw_data",
                     "bio.xlsx", b"bio data")
        assert r.status_code == 201

    def test_bio_upload_compound_info_rejected(self, server_url, bio_session, batch):
        r = _upload(server_url, bio_session, batch["id"], "compound_info",
                     "test.xlsx", b"test")
        assert r.status_code == 403

    def test_upload_invalid_file_type(self, server_url, leader_session, batch):
        r = _upload(server_url, leader_session, batch["id"], "invalid_type",
                     "test.xlsx", b"test")
        assert r.status_code == 400

    def test_upload_wrong_extension(self, server_url, leader_session, batch):
        r = _upload(server_url, leader_session, batch["id"], "bio_raw_data",
                     "test.pdf", b"test")
        assert r.status_code == 400

    def test_upload_large_file(self, server_url, leader_session, batch):
        content = b"x" * (11 * 1024 * 1024)
        r = _upload(server_url, leader_session, batch["id"], "compound_info",
                     "large.xlsx", content)
        assert r.status_code == 413

    def test_upload_to_deleted_batch(self, server_url, leader_session, batch):
        bid = batch["id"]
        leader_session.delete(f"{server_url}/api/batches/{bid}")
        r = _upload(server_url, leader_session, bid, "compound_info",
                     "test.xlsx", b"test")
        assert r.status_code == 404

    def test_upload_multiple_files(self, server_url, leader_session, batch):
        r = leader_session.post(
            f"{server_url}/api/batches/{batch['id']}/files",
            files=[
                ("file_type", (None, "compound_info")),
                ("file", ("a.xlsx", b"content a")),
                ("file", ("b.xlsx", b"content b")),
            ],
        )
        assert r.status_code == 201
        data = r.json()
        assert len(data["batch"]["files"]["compound_info"]["versions"]) == 2


class TestDownloadFile:
    def test_download_file(self, server_url, leader_session, batch):
        r = _upload(server_url, leader_session, batch["id"], "compound_info",
                     "download.xlsx", b"download content")
        file_id = r.json()["batch"]["files"]["compound_info"]["latest"]["id"]
        r2 = leader_session.get(f"{server_url}/api/files/{file_id}/download")
        assert r2.status_code == 200
        assert r2.content == b"download content"

    def test_download_nonexistent_file(self, server_url, leader_session):
        r = leader_session.get(f"{server_url}/api/files/99999/download")
        assert r.status_code == 404

    def test_download_deleted_file(self, server_url, leader_session, batch):
        r = _upload(server_url, leader_session, batch["id"], "compound_info",
                     "del.xlsx", b"to delete")
        file_id = r.json()["batch"]["files"]["compound_info"]["latest"]["id"]
        leader_session.delete(f"{server_url}/api/files/{file_id}")
        r2 = leader_session.get(f"{server_url}/api/files/{file_id}/download")
        assert r2.status_code == 404


class TestDeleteFile:
    def test_manager_delete_file(self, server_url, leader_session, batch):
        r = _upload(server_url, leader_session, batch["id"], "compound_info",
                     "delete.xlsx", b"delete me")
        file_id = r.json()["batch"]["files"]["compound_info"]["latest"]["id"]
        r2 = leader_session.delete(f"{server_url}/api/files/{file_id}")
        assert r2.status_code == 200
        assert r2.json()["ok"] is True

    def test_chem_can_delete_own_file(self, server_url, chem_session, batch):
        r = _upload(server_url, chem_session, batch["id"], "compound_info",
                     "chem_delete.xlsx", b"chem")
        file_id = r.json()["batch"]["files"]["compound_info"]["latest"]["id"]
        r2 = chem_session.delete(f"{server_url}/api/files/{file_id}")
        assert r2.status_code == 200

    def test_bio_cannot_delete_chem_file(self, server_url, bio_session, leader_session, batch):
        r = _upload(server_url, leader_session, batch["id"], "compound_info",
                     "leader.xlsx", b"leader")
        file_id = r.json()["batch"]["files"]["compound_info"]["latest"]["id"]
        r2 = bio_session.delete(f"{server_url}/api/files/{file_id}")
        assert r2.status_code == 403

    def test_delete_nonexistent_file(self, server_url, leader_session):
        r = leader_session.delete(f"{server_url}/api/files/99999")
        assert r.status_code == 404


class TestRestoreFile:
    def test_restore_file(self, server_url, leader_session, batch):
        r = _upload(server_url, leader_session, batch["id"], "compound_info",
                     "restore.xlsx", b"restore me")
        file_id = r.json()["batch"]["files"]["compound_info"]["latest"]["id"]
        leader_session.delete(f"{server_url}/api/files/{file_id}")
        r2 = leader_session.post(f"{server_url}/api/files/{file_id}/restore")
        assert r2.status_code == 200
        assert r2.json()["ok"] is True

    def test_non_manager_cannot_restore(self, server_url, chem_session, batch):
        r = _upload(server_url, chem_session, batch["id"], "compound_info",
                     "chem_restore.xlsx", b"chem")
        file_id = r.json()["batch"]["files"]["compound_info"]["latest"]["id"]
        chem_session.delete(f"{server_url}/api/files/{file_id}")
        r2 = chem_session.post(f"{server_url}/api/files/{file_id}/restore")
        assert r2.status_code == 403

    def test_restore_not_deleted_file(self, server_url, leader_session, batch):
        r = _upload(server_url, leader_session, batch["id"], "compound_info",
                     "active.xlsx", b"active")
        file_id = r.json()["batch"]["files"]["compound_info"]["latest"]["id"]
        r2 = leader_session.post(f"{server_url}/api/files/{file_id}/restore")
        assert r2.status_code == 404

    def test_restore_when_batch_deleted(self, server_url, leader_session, batch):
        r = _upload(server_url, leader_session, batch["id"], "compound_info",
                     "in_trash.xlsx", b"trash")
        file_id = r.json()["batch"]["files"]["compound_info"]["latest"]["id"]
        leader_session.delete(f"{server_url}/api/files/{file_id}")
        leader_session.delete(f"{server_url}/api/batches/{batch['id']}")
        r2 = leader_session.post(f"{server_url}/api/files/{file_id}/restore")
        assert r2.status_code == 409
