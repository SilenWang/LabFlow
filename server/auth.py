import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets

from server.config import SECRET_PATH
from server.utils import ensure_dirs


def get_secret():
    ensure_dirs()
    if not SECRET_PATH.exists():
        SECRET_PATH.write_bytes(secrets.token_bytes(32))
    return SECRET_PATH.read_bytes()


SECRET = get_secret()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 180000
    )
    return salt, base64.b64encode(digest).decode("ascii")


def verify_password(password, salt, expected):
    _, actual = password_hash(password, salt)
    return hmac.compare_digest(actual, expected)


def sign_payload(payload):
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    sig = hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_signed(token):
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except Exception:
        return None
    if payload.get("exp", 0) < dt.datetime.now().timestamp():
        return None
    return payload
