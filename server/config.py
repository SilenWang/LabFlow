from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = DATA_DIR / "labflow.db"
SECRET_PATH = DATA_DIR / "secret.key"
HOST = "0.0.0.0"
PORT = int(__import__("os").environ.get("LABFLOW_PORT", "8080"))

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
