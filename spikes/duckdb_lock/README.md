# D5 复核脚本：mcp-server-motherduck × DuckDB 独占锁

对应 VYB-419（D5 独立复核），结论写回 `docs/决策记录-数据库路线切换.md` 的 D-2。

```bash
pixi run lock-spike      # 锁行为全量实测（A 锁释放时机 / B 只读持锁 / C sqlite 对照 / D 官方 MCP）
pixi run lock-forms      # 三种绕锁形态的成本与新鲜度（快照 / HTTP 出口 / 按需 open-close）
```

留档输出：`logs/lock-spike.log`、`logs/lock-forms.log`。

## 脚本

| 文件 | 作用 |
| --- | --- |
| `run_all.py` | 编排 A–D 四组实测，stdout 即留档日志 |
| `forms_cost.py` | 三种绕锁形态的耗时 / 体积 / 滞后实测 |
| `holder.py` | 长驻"应用"进程：按 stdin 指令 open / close / begin / commit，用来定位锁的释放时刻 |
| `probe_duckdb.py` | 一次性探测：rw / read_only / `ATTACH (READ_ONLY)` / `lock_configuration=false` / 重试等待 |
| `probe_sqlite.py` | sqlite 对照：WAL + `busy_timeout` 下的读 / 写 / 排队 |
| `mcp_probe.py` | 真跑官方本地包 MCP 的 stdio 会话（initialize → tools/list → `execute_query`），可带 `--read-write` / `--ephemeral-connections` / `--no-ephemeral-connections` |
| `processes.py` | 两个脚本共用的子进程工具 |

## 环境

python 3.14.6 / duckdb 1.5.5 / duckdb-engine 0.17.0 / mcp-server-motherduck 1.0.8，全部由 `pixi.toml` 管。

夹具与临时产物写在 gitignore 的 `.tmp/` 下，可反复重跑。
