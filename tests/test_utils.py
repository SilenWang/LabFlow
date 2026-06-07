import pytest


@pytest.fixture
def utils(test_dir):
    from server.config import (
        BASE_DIR, DATA_DIR, UPLOAD_DIR, STATIC_DIR, DB_PATH, SECRET_PATH,
    )
    import server.config as cfg
    cfg.BASE_DIR = test_dir
    cfg.DATA_DIR = test_dir / "data"
    cfg.UPLOAD_DIR = test_dir / "uploads"
    cfg.STATIC_DIR = test_dir / "static"
    cfg.DB_PATH = test_dir / "data" / "labflow.db"
    cfg.SECRET_PATH = test_dir / "data" / "secret.key"
    (test_dir / "data").mkdir(parents=True, exist_ok=True)

    import server.auth as auth
    import importlib
    importlib.reload(auth)

    from server.validators import safe_filename, assert_date, clean_text
    from server.auth import password_hash, verify_password, sign_payload, read_signed
    from server.utils import now_iso, today_token
    return {
        "password_hash": password_hash,
        "verify_password": verify_password,
        "sign_payload": sign_payload,
        "read_signed": read_signed,
        "safe_filename": safe_filename,
        "assert_date": assert_date,
        "clean_text": clean_text,
        "now_iso": now_iso,
        "today_token": today_token,
    }


class TestPasswordHash:
    def test_hash_and_verify(self, utils):
        salt, hashed = utils["password_hash"]("test123")
        assert utils["verify_password"]("test123", salt, hashed)
        assert not utils["verify_password"]("wrong", salt, hashed)

    def test_different_salts(self, utils):
        salt1, h1 = utils["password_hash"]("same")
        salt2, h2 = utils["password_hash"]("same")
        assert salt1 != salt2
        assert h1 != h2

    def test_empty_password(self, utils):
        salt, hashed = utils["password_hash"]("")
        assert utils["verify_password"]("", salt, hashed)
        assert not utils["verify_password"]("x", salt, hashed)


class TestSignPayload:
    def test_sign_and_read(self, utils):
        payload = {"uid": 1, "exp": 9999999999}
        token = utils["sign_payload"](payload)
        result = utils["read_signed"](token)
        assert result is not None
        assert result["uid"] == 1

    def test_expired_token(self, utils):
        token = utils["sign_payload"]({"uid": 1, "exp": 1})
        assert utils["read_signed"](token) is None

    def test_tampered_token(self, utils):
        token = utils["sign_payload"]({"uid": 1, "exp": 9999999999})
        assert utils["read_signed"](token + "x") is None

    def test_invalid_token(self, utils):
        assert utils["read_signed"]("") is None
        assert utils["read_signed"]("invalid") is None
        assert utils["read_signed"]("a.b.c") is None


class TestSafeFilename:
    def test_basic(self, utils):
        assert utils["safe_filename"]("test.xlsx") == "test.xlsx"
        assert utils["safe_filename"]("") == "upload.xlsx"
        assert utils["safe_filename"](None) == "upload.xlsx"

    def test_sanitize(self, utils):
        assert "/" not in utils["safe_filename"]("a/b.txt")
        assert "\\" not in utils["safe_filename"]("a\\b.txt")
        assert "<" not in utils["safe_filename"]("a<b.txt")


class TestAssertDate:
    def test_valid_dates(self, utils):
        assert utils["assert_date"]("2024-01-15") == "2024-01-15"
        assert utils["assert_date"]("") is None
        assert utils["assert_date"](None) is None

    def test_invalid_dates(self, utils):
        with pytest.raises(ValueError, match="日期格式"):
            utils["assert_date"]("2024/01/15")
        with pytest.raises(ValueError, match="日期格式"):
            utils["assert_date"]("not-a-date")
        with pytest.raises(ValueError):
            utils["assert_date"]("2024-13-01")


class TestCleanText:
    def test_valid(self, utils):
        assert utils["clean_text"]("  hello  ") == "hello"
        assert utils["clean_text"]("") == ""
        assert utils["clean_text"](None) is None

    def test_max_length(self, utils):
        with pytest.raises(ValueError, match="不能超过"):
            utils["clean_text"]("x" * 130, max_len=120)


class TestNowIso:
    def test_format(self, utils):
        result = utils["now_iso"]()
        assert "T" in result
        assert "+" in result
