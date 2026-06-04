import datetime as dt
import re
from pathlib import Path


def safe_filename(name):
    stem = Path(name or "upload.xlsx").name
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    return stem or "upload.xlsx"


def assert_date(value):
    if value in (None, ""):
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        raise ValueError("日期格式必须是 YYYY-MM-DD")
    dt.date.fromisoformat(str(value))
    return str(value)


def clean_text(value, max_len=120):
    if value is None:
        return None
    value = str(value).strip()
    if len(value) > max_len:
        raise ValueError(f"文本不能超过 {max_len} 个字符")
    return value
