"""LabFlow MCP server：把 LabFlow 已有的 HTTP API 封装成 MCP 工具。

本进程**只走 HTTP**，不导入 `server.*`、不打开数据库文件（ducklake / sqlite）。
鉴权与软删除语义全部复用 API，MCP 侧不复制业务规则、不拼 SQL。
"""

__version__ = "0.1.0"
