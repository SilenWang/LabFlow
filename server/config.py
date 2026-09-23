from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = DATA_DIR / "labflow.db"
SECRET_PATH = DATA_DIR / "secret.key"
HOST = "0.0.0.0"
PORT = int(__import__("os").environ.get("LABFLOW_PORT", "9002"))
BASE_PATH = (__import__("os").environ.get("LABFLOW_BASE_PATH") or "").rstrip("/")

ROLES = {
    "manager": "总负责人",
    "chem": "化学部门",
    "bio": "生物部门",
}

DATE_FIELDS = {
    "synthesis_submitted_date": {"manager", "chem"},
    "synthesis_completed_date": {"manager", "chem"},
    "bio_test_start_date": {"manager", "bio"},
    "bio_test_completed_date": {"manager", "bio"},
}

# 文本上限：models 里的 VARCHAR 长度与 handler 的输入校验共用同一份，
# 避免两边漂移（例如输入放宽到 120 而列还停在 80）导致 seekdb 下报 1406。
TEXT_MAX_LENGTH = {
    "username": 60,
    "display_name": 60,
    "project_name": 80,
    "batch_no": 80,
    "name": 120,  # 批次名称
    "remark": 1000,
}

# 上传文件名上限（落库列 original_name 为 VARCHAR(255)，storage_path 还要再拼前缀）。
FILENAME_MAX_LENGTH = 200

TEXT_FIELDS = {
    "batch_no": {"manager", "chem"},
    "name": {"manager", "chem"},
    "project_id": {"manager"},
    "remark": {"manager", "chem"},
}

FILE_FIELDS = {
    "compound_info": {"manager", "chem"},
    "bio_raw_data": {"manager", "bio"},
    "data_summary": {"manager", "bio"},
    "experiment_record": {"manager", "chem", "bio"},
    "experiment_summary": {"manager", "chem", "bio"},
}

FILE_LABELS = {
    "compound_info": "化合物信息文件",
    "bio_raw_data": "生物原始数据文件",
    "data_summary": "数据整理文档",
    "experiment_record": "试验记录",
    "experiment_summary": "实验小结",
}

FILE_EXTENSIONS = {
    "compound_info": (".xlsx", ".xls", ".xlsm", ".csv", ".pdf", ".doc", ".docx", ".ppt", ".pptx"),
    "bio_raw_data": (".xlsx", ".xls", ".xlsm", ".csv"),
    "data_summary": (".xlsx", ".xls", ".xlsm", ".csv", ".pdf", ".doc", ".docx", ".ppt", ".pptx"),
    "experiment_record": (".xlsx", ".xls", ".xlsm", ".csv", ".pdf", ".doc", ".docx", ".ppt", ".pptx"),
    "experiment_summary": (".xlsx", ".xls", ".xlsm", ".csv", ".pdf", ".doc", ".docx", ".ppt", ".pptx"),
}
