"""MCP 工具定义。

原则：
- 每个工具背后都是 LabFlow 已有的一个 HTTP API 只读端点（映射见 mcp/README.md）。
- 不导入 server.*、不碰数据库文件、不拼 SQL、不新造查询语言。
- 失败一律抛 ToolError，让客户端拿到一句可读的中文原因，而不是崩溃/超时。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .client import LabFlowClient, LabFlowError
from .config import (
    API_BATCHES,
    API_FILE_CONFIG,
    API_ME,
    API_PROJECTS,
    API_TRASH,
    API_USERS,
    Settings,
)

READ_ONLY = ToolAnnotations(read_only_hint=True)
# 下载会在本机落一个文件副本，所以不能声称 read_only（MCP 规范里它指的是“不修改环境”）。
# 但它对 LabFlow 侧确实是只读，也不破坏任何东西，故 destructive_hint=False。
LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)

INSTRUCTIONS = """\
LabFlow 是实验室批次协同系统。本 MCP server 只读地封装了它的 HTTP API：
项目、批次、文件列表、回收站、账号、文件类型配置。

所有工具都复用应用的登录态与软删除语义（已删除的项目/批次/文件不会出现）。
本 server 不直连数据库，也没有任何写 LabFlow 数据的操作。"""


def _safe_component(name: str) -> str:
    """把服务端给的原始文件名收敛成单个安全文件名，不允许跳出目标目录。"""
    base = Path(name.replace("\\", "/")).name.strip().replace("\x00", "")
    return base if base not in {"", ".", ".."} else "download"


def create_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or Settings.from_env()
    client = LabFlowClient(settings)

    server = MCPServer(
        name="labflow",
        title="LabFlow",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    def call(path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return client.get_json(path, params=params)
        except LabFlowError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="labflow_health",
        title="LabFlow 连通性检查",
        description=(
            "检查 LabFlow 应用是否可达、账号能否登录，并返回当前登录身份。"
            "其它工具报错时先用它定位是应用没起还是账号配置问题。"
            "对应 API：GET /api/me。"
        ),
        annotations=READ_ONLY,
    )
    def labflow_health() -> dict[str, Any]:
        return client.health()

    @server.tool(
        name="labflow_current_user",
        title="当前登录账号",
        description="返回本 MCP server 使用的 LabFlow 账号及其角色。对应 API：GET /api/me。",
        annotations=READ_ONLY,
    )
    def labflow_current_user() -> dict[str, Any]:
        return call(API_ME)

    @server.tool(
        name="labflow_list_projects",
        title="列出项目",
        description=(
            "列出所有未删除的项目（id / 名称 / 创建时间，按名称排序）。"
            "对应 API：GET /api/projects。"
        ),
        annotations=READ_ONLY,
    )
    def labflow_list_projects() -> dict[str, Any]:
        return call(API_PROJECTS)

    @server.tool(
        name="labflow_list_batches",
        title="列出批次",
        description=(
            "列出所有未删除的批次，含日期字段与每个批次的文件版本列表。"
            "project_id 传入时只返回该项目下的批次，传 0 或不传表示全部。"
            "对应 API：GET /api/batches（可带 project_id 查询参数）。"
        ),
        annotations=READ_ONLY,
    )
    def labflow_list_batches(project_id: int | None = None) -> dict[str, Any]:
        params = {"project_id": project_id} if project_id else None
        return call(API_BATCHES, params=params)

    @server.tool(
        name="labflow_get_batch",
        title="查看单个批次",
        description=(
            "按 batch_id 返回一个未删除批次的完整信息（含文件列表）。"
            "实现上复用 GET /api/batches 的返回，只在结果里按 id 取一条，"
            "软删除过滤仍由服务端完成，没有新增查询语义。"
        ),
        annotations=READ_ONLY,
    )
    def labflow_get_batch(batch_id: int) -> dict[str, Any]:
        payload = call(API_BATCHES)
        for batch in payload.get("batches") or []:
            if batch.get("id") == batch_id:
                return {"batch": batch}
        raise ToolError(
            f"批次 {batch_id} 不存在或已被删除。可用 labflow_list_batches 查看现有批次。"
        )

    @server.tool(
        name="labflow_list_trash",
        title="列出回收站",
        description=(
            "列出回收站里的项目、批次、文件（只读，不会恢复任何东西）。"
            "需要 manager（总负责人）角色账号，否则 API 会返回 403。"
            "对应 API：GET /api/trash。"
        ),
        annotations=READ_ONLY,
    )
    def labflow_list_trash() -> dict[str, Any]:
        return call(API_TRASH)

    @server.tool(
        name="labflow_list_users",
        title="列出账号",
        description=(
            "列出系统账号及角色。需要 manager（总负责人）角色账号，否则 API 会返回 403。"
            "对应 API：GET /api/users。"
        ),
        annotations=READ_ONLY,
    )
    def labflow_list_users() -> dict[str, Any]:
        return call(API_USERS)

    @server.tool(
        name="labflow_file_config",
        title="文件类型配置",
        description=(
            "返回五类文件的中文标签与允许的扩展名。对应 API：GET /api/file-config。"
        ),
        annotations=READ_ONLY,
    )
    def labflow_file_config() -> dict[str, Any]:
        return call(API_FILE_CONFIG)

    def _download_file(file_id: int, save_dir: str | None = None) -> dict[str, Any]:
        """下载一个文件版本到本机。只在 LABFLOW_MCP_ENABLE_DOWNLOAD 打开时注册。"""
        path = f"/api/files/{int(file_id)}/download"
        response = None
        try:
            response = client.get_stream(path)
            disposition = response.headers.get("Content-Disposition", "")
            marker = "filename*=UTF-8''"
            raw_name = disposition.split(marker, 1)[1].strip().strip('"') if marker in disposition else ""
            name = _safe_component(unquote(raw_name)) if raw_name else f"file-{int(file_id)}"

            target_dir = Path(save_dir).expanduser() if save_dir else settings.download_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / name
            with target.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    fh.write(chunk)
        except LabFlowError as exc:
            raise ToolError(str(exc)) from exc
        except OSError as exc:
            raise ToolError(f"写入下载目录失败：{exc}") from exc
        finally:
            if response is not None:
                response.close()

        return {
            "file_id": int(file_id),
            "original_name": name,
            "saved_to": str(target),
            "size_bytes": target.stat().st_size,
        }

    if settings.enable_download:
        # 默认不注册：它会往本机落文件，按“只读优先、确有需要再逐个开”的口径，
        # 需要的人显式设 LABFLOW_MCP_ENABLE_DOWNLOAD=1 才拿到这个工具。
        server.add_tool(
            _download_file,
            name="labflow_download_file",
            title="下载文件",
            description=(
                "把某个文件版本下载到 MCP 所在机器（对 LabFlow 是只读，但会在本机写一个副本）。"
                "file_id 取自批次文件列表里的 id。默认存到 LABFLOW_MCP_DOWNLOAD_DIR，"
                "可用 save_dir 覆盖。对应 API：GET /api/files/{file_id}/download。"
            ),
            annotations=LOCAL_WRITE,
        )

    return server
