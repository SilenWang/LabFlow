"""LABFLOW_DB 取值校验：写错后端名要当场报错，不能静默落到 sqlite。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _import_config(backend):
    return subprocess.run(
        [sys.executable, "-c", "import server.config as c; print(c.DB_BACKEND)"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "LABFLOW_DB": backend},
    )


def test_unknown_backend_is_rejected():
    """老名字 duckdb（D1 验收实测）以前会静默落到 sqlite 分支，起来看着能用、其实换了库。"""
    result = _import_config("duckdb")
    assert result.returncode != 0, result.stdout
    assert "LABFLOW_DB" in result.stderr
    assert "sqlite" in result.stderr, "报错要列出可选值"


@pytest.mark.parametrize("backend", ["sqlite", "ducklake"])
def test_known_backends_are_accepted(backend):
    result = _import_config(backend)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == backend
