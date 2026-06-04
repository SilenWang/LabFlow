import datetime as dt


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def today_token():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dirs():
    from server.config import DATA_DIR, UPLOAD_DIR, STATIC_DIR

    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    STATIC_DIR.mkdir(exist_ok=True)


def quote_bytes(value):
    return "".join(
        chr(b) if 0x30 <= b <= 0x39 or 0x41 <= b <= 0x5A or 0x61 <= b <= 0x7A else f"%{b:02X}"
        for b in value
    )
