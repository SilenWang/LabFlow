import socket
import threading
import tempfile
import shutil
from pathlib import Path

import pytest
import requests as req

# Monkey-patch password_hash BEFORE any server module is loaded
import hashlib
import base64
import secrets as _secrets


def _fast_password_hash(password, salt=None):
    salt = salt or _secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 1
    )
    return salt, base64.b64encode(digest).decode("ascii")


# Patch auth module
import server.auth as auth_mod
auth_mod.password_hash = _fast_password_hash

# Patch db module's local reference
import server.db as db_mod
db_mod.password_hash = _fast_password_hash

import server.config as cfg
from server.handler import LabFlowHandler
from http.server import ThreadingHTTPServer


def _patch_config_refs(test_dir):
    """Patch module-local config references so they pick up the test paths."""
    data_dir = test_dir / "data"
    upload_dir = test_dir / "uploads"
    static_dir = test_dir / "static"
    db_path = data_dir / "labflow.db"
    secret_path = data_dir / "secret.key"

    data_dir.mkdir(parents=True, exist_ok=True)
    upload_dir.mkdir(parents=True, exist_ok=True)
    static_dir.mkdir(parents=True, exist_ok=True)

    # Update config module globals
    cfg.BASE_DIR = test_dir
    cfg.DATA_DIR = data_dir
    cfg.UPLOAD_DIR = upload_dir
    cfg.STATIC_DIR = static_dir
    cfg.DB_PATH = db_path
    cfg.SECRET_PATH = secret_path
    cfg.HOST = "127.0.0.1"

    # Patch db module's local references (it uses `from server.config import DB_PATH`)
    db_mod.DB_PATH = db_path

    # Re-init auth secret
    import server.utils as utils_mod
    utils_mod.ensure_dirs()
    auth_mod.SECRET = auth_mod.get_secret()

    return data_dir, upload_dir, static_dir, db_path


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="function")
def test_dir():
    tmp = Path(tempfile.mkdtemp(prefix="labflow_test_"))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="function")
def server_url(test_dir):
    data_dir, upload_dir, static_dir, db_path = _patch_config_refs(test_dir)

    port = _free_port()
    cfg.PORT = port

    real_static = Path(__file__).resolve().parent.parent / "static"
    if real_static.exists():
        for f in real_static.iterdir():
            shutil.copy2(f, static_dir / f.name)

    db_mod.init_db()

    server = ThreadingHTTPServer(("127.0.0.1", port), LabFlowHandler)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    url = f"http://127.0.0.1:{port}"
    yield url

    server.shutdown()


@pytest.fixture
def client():
    return req.Session()


def _make_session(server_url, username, password):
    session = req.Session()
    session.headers.update({"Accept": "application/json"})
    r = session.post(f"{server_url}/api/login", json={
        "username": username,
        "password": password,
    })
    assert r.status_code == 200, f"Login failed for {username}: {r.text}"
    return session


@pytest.fixture
def leader_session(server_url):
    return _make_session(server_url, "leader", "labflow123")


@pytest.fixture
def chem_session(server_url):
    return _make_session(server_url, "chem1", "chem123")


@pytest.fixture
def bio_session(server_url):
    return _make_session(server_url, "bio1", "bio123")


@pytest.fixture
def project(leader_session, server_url):
    r = leader_session.post(f"{server_url}/api/projects", json={"name": "测试项目"})
    data = r.json()
    yield data["project"]


@pytest.fixture
def batch(leader_session, server_url, project):
    r = leader_session.post(f"{server_url}/api/batches", json={
        "project_id": project["id"],
        "batch_no": "BATCH-001",
        "name": "测试批次-001",
    })
    data = r.json()
    yield data["batch"]
