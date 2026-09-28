#!/usr/bin/env python
"""LabFlow MCP server 入口（stdio）。

  pixi run mcp

客户端（Claude Desktop / Cursor / 其它 MCP 客户端）把它当子进程拉起，
用 stdin/stdout 说话。配置见 mcp/README.md。

放在 mcp/ 下而不是做成 `python -m mcp.xxx`：`mcp` 是官方 SDK 的顶层包名，
若把本目录（或子包）挂到 `mcp.` 前缀下就会遮蔽 site-packages 里的 `mcp`。
这里以脚本方式启动，sys.path[0] 是 mcp/ 目录，只暴露 `labflow_mcp`，不与 SDK 撞名。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labflow_mcp.server import create_server  # noqa: E402


def main() -> None:
    create_server().run("stdio")


if __name__ == "__main__":
    main()
