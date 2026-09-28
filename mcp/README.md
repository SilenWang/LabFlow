# LabFlow MCP server

把 LabFlow 已有的 HTTP API 封装成 MCP 工具，供 AI 客户端（Claude Desktop、Cursor、其它支持 MCP 的客户端）只读访问实验室数据。

## 为什么是这一条链

之前评估的「本地 MCP 直连数据库」方案绕不开 DuckDB 的**独占文件锁**：应用持着常驻连接时，第二个进程连读都打不开（实测见 `docs/决策记录-数据库路线切换.md` D5 与 `spikes/duckdb_lock/`）。

本目录走另一条路：**MCP 进程永不碰数据库文件，只通过 HTTP 调本机正在跑的 LabFlow 应用**。

- 鉴权、权限、软删除语义全部复用 API，MCP 侧一行业务规则都没有；
- 没有新增开库进程，也就不存在抢锁问题；
- AI 与应用看的是同一份数据、同一套权限，不存在两套口径。

本目录**不导入 `server/`**，也不修改 `server/` 里的任何代码。新增只读能力时，先在 API 侧加端点，再来这里加工具。

## 前置条件

1. LabFlow 应用在跑（`pixi run serve`，或 systemd 托管的 `labflow` 服务）；
2. 有一个可用的 LabFlow 账号（`LABFLOW_MCP_USERNAME` / `LABFLOW_MCP_PASSWORD`）。

## 启动

stdio 传输，由 MCP 客户端把本进程作为子进程拉起。手动跑一次看它能否起来：

```bash
LABFLOW_API_URL=http://127.0.0.1:9002 \
LABFLOW_MCP_USERNAME=leader \
LABFLOW_MCP_PASSWORD=labflow123 \
pixi run mcp
```

它会等 stdin 上的 MCP 消息，直接在前台跑不会有输出，Ctrl+C 退出。想验证协议是否正常，用下面的客户端配置接一个客户端，或跑测试：

```bash
pixi run test -k mcp
```

## 配到客户端

以 Claude Desktop 的 `claude_desktop_config.json` 为例（Cursor / 其它客户端的 `mcpServers` 结构相同）：

```json
{
  "mcpServers": {
    "labflow": {
      "command": "pixi",
      "args": [
        "run",
        "--manifest-path", "/绝对路径/LabFlow/pixi.toml",
        "mcp"
      ],
      "env": {
        "LABFLOW_API_URL": "http://127.0.0.1:9002",
        "LABFLOW_MCP_USERNAME": "leader",
        "LABFLOW_MCP_PASSWORD": "改成真实密码"
      }
    }
  }
}
```

客户端的环境里不一定有 `pixi`，那也可以直接指向 pixi 环境里的解释器（路径随平台不同，`pixi run which python` 可查）：

```json
{
  "mcpServers": {
    "labflow": {
      "command": "/绝对路径/LabFlow/.pixi/envs/default/bin/python",
      "args": ["/绝对路径/LabFlow/mcp/run.py"],
      "env": {
        "LABFLOW_API_URL": "http://127.0.0.1:9002",
        "LABFLOW_MCP_USERNAME": "leader",
        "LABFLOW_MCP_PASSWORD": "改成真实密码"
      }
    }
  }
}
```

环境变量示例见 `mcp/mcp.env.example`。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `LABFLOW_API_URL` | 否 | `http://127.0.0.1:9002` | 应用地址。端口与 `deploy/labflow.env` 的 `LABFLOW_PORT` 一致。子路径部署时写全，如 `http://host:9002/labflow`。 |
| `LABFLOW_MCP_USERNAME` | 是 | 无 | 用哪个账号访问。只读工具按该账号权限走。 |
| `LABFLOW_MCP_PASSWORD` | 是 | 无 | 该账号密码。别提交进仓库。 |
| `LABFLOW_MCP_TIMEOUT` | 否 | `10` | 单次 HTTP 超时（秒）。应用没起时靠它快速失败，不会挂死。 |
| `LABFLOW_MCP_ENABLE_DOWNLOAD` | 否 | 关 | 设成 `1` 才注册 `labflow_download_file`。见下文「默认没开的东西」。 |
| `LABFLOW_MCP_DOWNLOAD_DIR` | 否 | `mcp-downloads` | `labflow_download_file` 的默认落盘目录。相对路径按 MCP server 的工作目录算。 |

## 工具 → API 端点

默认注册的 8 个工具全部只读。实现就是把对应端点的 JSON 原样（或截取一条）交给模型，没有任何 MCP 侧过滤规则。

| 工具 | 对应的 API | 说明 |
| --- | --- | --- |
| `labflow_health` | `GET /api/me` | 探活：能否连上、能否登录、当前身份。报错时先用它定位。 |
| `labflow_current_user` | `GET /api/me` | 当前账号与角色。 |
| `labflow_list_projects` | `GET /api/projects` | 未删除的项目。 |
| `labflow_list_batches` | `GET /api/batches`（可选 `project_id`） | 未删除的批次，含日期字段与文件版本列表。 |
| `labflow_get_batch` | `GET /api/batches` | 按 id 取一条。软删除过滤仍由服务端完成，这里只在返回结果里按键取值。 |
| `labflow_list_trash` | `GET /api/trash` | 回收站（项目 / 批次 / 文件）。需要 `manager`。 |
| `labflow_list_users` | `GET /api/users` | 账号列表。需要 `manager`。 |
| `labflow_file_config` | `GET /api/file-config` | 五类文件的中文标签与允许扩展名。 |

权限不在 MCP 侧判断：`chem` / `bio` 调 `manager` 专属工具时，直接透传 API 的 403 文案。

## 默认没开的东西

**写操作全都没开**：建项目 / 建批次 / 改字段 / 上传 / 删除 / 恢复一个都没有。要开就得单独说明理由、按需逐个加。

**`labflow_download_file` 默认不注册**（`GET /api/files/{file_id}/download`）。它对 LabFlow 是只读，但会在 MCP 所在机器上落一个文件副本——按「只读优先、确有需要再逐个开」的口径，需要的人显式打开：

```bash
LABFLOW_MCP_ENABLE_DOWNLOAD=1 pixi run mcp
```

打开后它在 `tools/list` 里如实标成 `readOnlyHint=false`（`destructiveHint=false`），客户端可以据此决定要不要弹确认。落盘文件名取自 API 的 `Content-Disposition`，只取最后一段并挡掉 `.` / `..`，不会跳出 `LABFLOW_MCP_DOWNLOAD_DIR`。

**「任意 SQL 查询」刻意不做**：真需要就先在 API 侧加只读端点，再在这里加工具——不是让 MCP 去开库。

## 怎么确认它没碰数据库

MCP 进程只依赖 `requests` 与 MCP SDK，不导入 `server.*`。要自证，可以在它持有会话时看进程打开了哪些文件：

```bash
pixi run mcp &            # 或由客户端拉起
MCP_PID=$(pgrep -f "mcp/run.py")
lsof -p "$MCP_PID" | grep -E "ducklake|\.db|\.duckdb"   # 应当无输出
```

`tests/test_mcp_server.py` 里有端到端用例，对着真实 HTTP 服务跑 tools/list 与只读工具调用。
