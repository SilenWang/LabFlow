"""MCP server 的端到端测试。

用官方 mcp 客户端 SDK 以 stdio 拉起 `mcp/run.py`，对着 conftest 起的
真实 HTTP 服务跑一遍：tools/list、只读工具调用、权限透传、应用未运行时的报错。

这里刻意不 mock HTTP：要验证的正是“MCP 进程只走 HTTP、语义全由 API 决定”。
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_ENTRY = REPO_ROOT / "mcp" / "run.py"

READ_ONLY_TOOLS = {
    "labflow_health",
    "labflow_current_user",
    "labflow_list_projects",
    "labflow_list_batches",
    "labflow_get_batch",
    "labflow_list_trash",
    "labflow_list_users",
    "labflow_file_config",
}

DOWNLOAD_TOOL = "labflow_download_file"


def _server_params(api_url: str, download_dir: Path, username="leader", password="labflow123",
                   enable_download=False):
    env = {
        "LABFLOW_API_URL": api_url,
        "LABFLOW_MCP_USERNAME": username,
        "LABFLOW_MCP_PASSWORD": password,
        "LABFLOW_MCP_DOWNLOAD_DIR": str(download_dir),
        "LABFLOW_MCP_TIMEOUT": "5",
    }
    if enable_download:
        env["LABFLOW_MCP_ENABLE_DOWNLOAD"] = "1"
    return StdioServerParameters(
        command=sys.executable,
        args=[str(MCP_ENTRY)],
        cwd=str(REPO_ROOT),
        env=env,
    )


def _payload(result) -> dict:
    """工具返回值：优先取 structured_content，退回到文本块里的 JSON。"""
    if result.structured_content is not None:
        return result.structured_content
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"工具没有返回可解析的内容: {result!r}")


def _text(result) -> str:
    return " ".join(getattr(b, "text", "") or "" for b in result.content)


async def _run(params: StdioServerParameters, calls: list[tuple[str, dict]]):
    """一次 stdio 会话：initialize -> tools/list -> 逐个 tools/call。

    返回 (tools, [(result, elapsed_ms)])。
    """
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            results = []
            for name, arguments in calls:
                started = time.monotonic()
                result = await session.call_tool(name, arguments)
                results.append((result, (time.monotonic() - started) * 1000))
            return listed.tools, results


def _call(api_url, download_dir, calls, **kwargs):
    params = _server_params(api_url, download_dir, **kwargs)
    return anyio.run(_run, params, calls)


def _free_closed_port() -> int:
    """拿一个刚释放、当前没人监听的端口，用来模拟“应用没在跑”。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_default_tool_surface_is_read_only_only(server_url, tmp_path):
    """默认不注册任何会改动环境的工具：tools/list 全是 readOnlyHint=true。"""
    tools, _ = _call(server_url, tmp_path, [])
    names = {t.name for t in tools}
    assert names == READ_ONLY_TOOLS
    assert DOWNLOAD_TOOL not in names
    for tool in tools:
        assert tool.annotations is not None and tool.annotations.read_only_hint is True, tool.name


def test_read_only_tools_return_real_data(server_url, leader_session, tmp_path, project, batch):
    leader_session.post(
        f"{server_url}/api/batches/{batch['id']}/files",
        files={"file_type": (None, "compound_info"), "file": ("cmp.xlsx", b"payload")},
    )

    _, results = _call(
        server_url,
        tmp_path,
        [
            ("labflow_health", {}),
            ("labflow_current_user", {}),
            ("labflow_list_projects", {}),
            ("labflow_list_batches", {"project_id": project["id"]}),
            ("labflow_get_batch", {"batch_id": batch["id"]}),
            ("labflow_get_batch", {"batch_id": 999999}),
        ],
    )

    health, me, projects, batches, one, missing = (r for r, _ in results)

    assert not health.is_error
    health_data = _payload(health)
    assert health_data["ok"] is True and health_data["user"]["username"] == "leader"

    assert _payload(me)["user"]["role"] == "manager"
    assert any(p["id"] == project["id"] for p in _payload(projects)["projects"])

    listed = _payload(batches)["batches"]
    assert [b["id"] for b in listed] == [batch["id"]]
    assert listed[0]["files"]["compound_info"]["latest"]["original_name"] == "cmp.xlsx"

    assert _payload(one)["batch"]["id"] == batch["id"]

    # 不存在的批次：清晰报错，而不是崩溃
    assert missing.is_error
    assert "999999" in _text(missing)


def test_permission_is_enforced_by_the_api(server_url, chem_session, tmp_path, project, batch):
    """MCP 侧不复制权限规则：chem 调 manager-only 工具，直接透传 API 的 403 文案。"""
    _, results = _call(
        server_url,
        tmp_path,
        [("labflow_list_users", {}), ("labflow_list_trash", {}), ("labflow_list_projects", {})],
        username="chem1",
        password="chem123",
    )
    users, trash, projects = (r for r, _ in results)

    assert users.is_error and "只有总负责人" in _text(users)
    assert trash.is_error and "只有总负责人" in _text(trash)
    assert not projects.is_error and any(p["id"] == project["id"] for p in _payload(projects)["projects"])


def test_download_tool_is_opt_in_and_saves_the_original_bytes(server_url, leader_session, tmp_path, batch):
    leader_session.post(
        f"{server_url}/api/batches/{batch['id']}/files",
        files={"file_type": (None, "compound_info"), "file": ("cmp.xlsx", b"payload-bytes")},
    )
    listed = leader_session.get(f"{server_url}/api/batches?project_id=all").json()["batches"]
    file_id = listed[0]["files"]["compound_info"]["latest"]["id"]

    params = _server_params(server_url, tmp_path, enable_download=True)
    tools, results = anyio.run(_run, params, [(DOWNLOAD_TOOL, {"file_id": file_id})])

    names = {t.name for t in tools}
    assert names == READ_ONLY_TOOLS | {DOWNLOAD_TOOL}
    # 它会在本机落盘，所以如实标成非只读（对 LabFlow 侧仍是读）
    download_tool = next(t for t in tools if t.name == DOWNLOAD_TOOL)
    assert download_tool.annotations.read_only_hint is False
    assert download_tool.annotations.destructive_hint is False

    result, _ = results[0]
    assert not result.is_error, _text(result)
    saved = _payload(result)
    assert saved["original_name"] == "cmp.xlsx"
    assert Path(saved["saved_to"]).read_bytes() == b"payload-bytes"


def test_disabled_download_tool_is_rejected_clearly(server_url, tmp_path):
    """没开开关时调下载工具：报“工具不存在”，而不是静默失败。"""
    _, results = _call(server_url, tmp_path, [(DOWNLOAD_TOOL, {"file_id": 1})])
    result, _ = results[0]
    assert result.is_error
    assert DOWNLOAD_TOOL in _text(result)


def test_app_not_running_reports_clearly_and_fast(server_url, tmp_path):
    """应用没起时：报错里点明地址与原因，且立刻返回（不挂死、不崩溃）。"""
    port = _free_closed_port()
    _, results = _call(f"http://127.0.0.1:{port}", tmp_path, [("labflow_list_projects", {})])
    result, elapsed_ms = results[0]

    assert result.is_error, "应用不可达时必须返回 is_error 结果"
    text = _text(result)
    assert f"127.0.0.1:{port}" in text
    assert "无法连接 LabFlow" in text
    assert elapsed_ms < 3000, f"应当在超时前快速失败，实际 {elapsed_ms:.0f}ms"


def test_missing_credentials_reports_clearly(server_url, tmp_path):
    """没配账号时也要一句话说清怎么修，而不是让模型猜。"""
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(MCP_ENTRY)],
        cwd=str(REPO_ROOT),
        env={"LABFLOW_API_URL": server_url, "LABFLOW_MCP_USERNAME": "", "LABFLOW_MCP_PASSWORD": ""},
    )
    _, results = anyio.run(_run, params, [("labflow_list_projects", {})])
    result, _ = results[0]
    assert result.is_error
    assert "LABFLOW_MCP_USERNAME" in _text(result)


def test_unknown_tool_is_rejected(server_url, tmp_path):
    _, results = _call(server_url, tmp_path, [("labflow_nope", {})])
    result, _ = results[0]
    assert result.is_error
    assert "labflow_nope" in _text(result)
