"""运行期配置，全部来自环境变量。

凭据不落盘、不写进 pixi.toml：MCP 客户端在拉起本进程时用 env 传进来。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_API_URL = "http://127.0.0.1:9002"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_DOWNLOAD_DIR = "mcp-downloads"

TRUTHY = {"1", "true", "yes", "on"}

# 与 README 的“工具 → API 端点”表共用；改端点时只改这里。
API_LOGIN = "/api/login"
API_ME = "/api/me"
API_FILE_CONFIG = "/api/file-config"
API_USERS = "/api/users"
API_TRASH = "/api/trash"
API_PROJECTS = "/api/projects"
API_BATCHES = "/api/batches"


@dataclass(frozen=True)
class Settings:
    api_url: str
    username: str
    password: str
    timeout: float
    download_dir: Path
    enable_download: bool

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        raw_timeout = (env.get("LABFLOW_MCP_TIMEOUT") or "").strip()
        try:
            timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
        except ValueError:
            timeout = DEFAULT_TIMEOUT_SECONDS
        download_dir = (env.get("LABFLOW_MCP_DOWNLOAD_DIR") or "").strip()
        enable_download = (env.get("LABFLOW_MCP_ENABLE_DOWNLOAD") or "").strip().lower()
        return cls(
            api_url=(env.get("LABFLOW_API_URL") or DEFAULT_API_URL).strip().rstrip("/"),
            username=(env.get("LABFLOW_MCP_USERNAME") or "").strip(),
            password=env.get("LABFLOW_MCP_PASSWORD") or "",
            timeout=timeout,
            download_dir=Path(download_dir or DEFAULT_DOWNLOAD_DIR).expanduser(),
            enable_download=enable_download in TRUTHY,
        )
