# DL0 DuckLake 模型层可行性核实（VYB-421）

## 结论

**模型层不能原样落到 DuckLake 上。** DuckLake 表的 DDL 里放不下 PRIMARY KEY / UNIQUE / FOREIGN KEY /
CHECK / sequence 中的任何一个（NOT NULL 除外），所以：

1. **D1 给 duckdb 定的 `Sequence` + `server_default=seq.next_value()` 写法在 DuckLake 上直接建不出表** ——
   报 `Not implemented Error: DuckLake does not support sequences`；`create_all` 连第一步 `CREATE SEQUENCE` 都过不去。
2. **自增主键只能改成应用层生成。** 没有 DB 生成、也没有唯一约束兜底，重复 id 会静默落库（实测两条 id=1）。
3. **"批次名全系统唯一（含回收站）" 的语义只能搬到应用层**（插入前 `SELECT` 校验），DuckLake 不提供任何
   DB 级保证；取消删除标记再新建同名批次，当前语义保持不变，但"两人同时建同名批次"从"必然 409"退化成"可能都成功"。
4. **查询形状不受影响**：回收站那条 subquery + outerjoin + coalesce、`Query.update()` 批量更新、
   `serializers` 里的 join 全部照跑；`create_all` 只要把约束从建表语句里摘掉就能幂等成型。

一句话：**DuckLake 换掉的不只是引擎，是模型层的一整层保证；D1 必须返工一版**（要改哪几处见下），
sqlite / seekdb 两个后端不受影响。

## 怎么复现

```bash
pixi run dl0-spike     # spikes/dl0_ducklake_models.py，输出留档 spikes/logs/dl0-ducklake.log
```

环境：duckdb **1.5.5**（ducklake 扩展版本 `d8a1881e`）/ duckdb-engine 0.17.0 / SQLAlchemy 2.0.50 / Python 3.14.6。
用的是**仓库真实的 `server/models.py`**（现状 + D1 分支 `a3b7de1` 原文）和**真实的查询形状**，不是玩具模型。
测试脚本每次从干净目录起，catalog 用 `ducklake:sqlite:<file>`。

## 官方口径 vs 本机实测

| 能力 | DuckLake 官方说法（出处见下） | 本机实测（duckdb 1.5.5 / ducklake `d8a1881e`） |
| --- | --- | --- |
| 约束总览 | "DuckLake has limited support for constraints. The only constraint type that is currently supported is **NOT NULL**. It does not support PRIMARY KEY, FOREIGN KEY, UNIQUE or CHECK constraints." | 完全一致 |
| PRIMARY KEY | 同上；另外明确写"enforced unique constraints / PK / FK **unlikely to be supported**" | `Not implemented Error: PRIMARY KEY/UNIQUE constraints are not supported in DuckLake` |
| UNIQUE | 同上 | 同上；`INSERT` 同名批次**成功**，库里两条同名行 |
| FOREIGN KEY | 同上 | `Binder Error: Failed to create foreign key: there is no primary key or unique constraint for referenced table`；`INSERT` 孤儿行（project_id=999）**成功** |
| CHECK | 同上（"likely to be supported in the future"） | `not supported` |
| SEQUENCE / 自增 | "Sequences" 列在 **Unlikely to Be Supported in the Future**；"Upserting is only supported via MERGE INTO since primary keys are not supported" | `Not implemented Error: DuckLake does not support sequences`；`create_all` 想建 `users_id_seq` 即失败 |
| 非字面量 DEFAULT（`nextval(...)`） | "Default values that are not literals" 不支持 | 走 sequence 那条路，同样报错 |
| NOT NULL | 支持 | 支持；违反照常抛 `IntegrityError`，rollback 后连接可继续用 |

出处：

- 约束页 <https://ducklake.select/docs/stable/duckdb/advanced_features/constraints>（"limited support for constraints …
  only … NOT NULL"）
- 不支持清单 <https://ducklake.select/docs/stable/duckdb/unsupported_features>（Sequences / Indexes /
  "Primary key or enforced unique constraints and foreign key constraints are unlikely to be supported" /
  "Default values that are not literals"）

## 逐条回答 issue 里的问题

**1. 自增主键：D1 的写法在 DuckLake 上不行，替代做法与代价**

| 做法 | 实测结果 | 代价 |
| --- | --- | --- |
| 现状 `autoincrement=True` | `Catalog Error: Type with name SERIAL does not exist`（连裸 DuckDB 都过不去） | — |
| D1 的 `Sequence` + `server_default=nextval` | `CREATE SEQUENCE users_id_seq` 即报 `DuckLake does not support sequences` | 写法作废 |
| 应用层 `max(id)+1` | 能跑，17.4 ms/次（含 INSERT 的一次事务）；**有竞态**：会话 A、B 都读到 max=33 → 都写 id=34，无约束拦 | 最省事，最不安全 |
| DuckLake 内计数表 | `UPDATE … RETURNING v` **不支持**（`RETURNING clause not yet supported for updates of a DuckLake table`）；退化成 `UPDATE`+`SELECT` 是 22.4 ms/次，且与其他写事务抢同一行（D5/D6 已测出 DuckLake 并发提交要靠应用层重试） | 不推荐 |
| **辅助 SQLite 计数表**（`seq(name PRIMARY KEY, next)` + `INSERT … ON CONFLICT DO UPDATE … RETURNING`） | **0.038 ms/次；4 线程各取 100 个 id：400/400 唯一、0 报错**（WAL + busy_timeout） | 多一个几十行的文件，catalog 本来就是 SQLite |

**2. 唯一约束 / 外键：建不了，也不生效；"批次名全系统唯一（含回收站）"靠应用层**

- DDL 建不出来（见上表）；即使不建约束，插入重名批次、孤儿外键、重复主键**全部静默成功**（实测留档）。
- 语义要靠应用层：插入前 `SELECT 1 FROM batches WHERE name = ?`（**不看 `deleted_at`**，保持"含回收站"），
  实测 8.2 ms/次；外键侧 `create_batch` 本来就已经查过 `project`，这条现有代码已覆盖大部分。
- 这样保住的是**语义**，保不住的是**保证**：校验与 `INSERT` 提交之间有窗口，两个进程可同时通过。
  想要 DB 级硬保证只有一条路——把唯一键放进一个辅助 SQLite 文件（唯一索引），但就成了双写、没有跨库事务。
- 顺带：D1 给 duckdb 补的 409 文案映射（`handler.py` 里按触发语句猜表名那段）在 **DuckLake 上是死代码**，
  永远不会触发 `IntegrityError`；409 必须由应用层校验抛 `RequestError`。

**3. 现有查询形状：全部照跑**（脚本第 3 段，用真实 models 建表后跑）

- `get_batch`（join Project + 软删过滤）、`latest_files`（join User + `desc` 排序 + 分组）、`list_batches`（join + order by）
- 回收站那条 `subquery + outerjoin + coalesce`：返回 `[(2, '项目乙（回收站）', 1)]`，与 sqlite 一致
- `Query.update()` 批量软删：成功（`rowcount` 仍是 duckdb-engine 的 `-1`，仓库里没人依赖它）
- 事务语义：NOT NULL 违反照常抛错，`rollback()` 后同一连接可继续插入

**4. `Base.metadata.create_all` 一次成型：可以，但必须先摘掉约束**

三版对比（脚本第 2 段）：

| 版本 | `create_all` 结果 |
| --- | --- |
| 现状 main（`autoincrement=True`） | `Catalog Error: Type with name SERIAL does not exist` |
| D1 `a3b7de1`（`Sequence` + `nextval`） | `Not implemented Error: DuckLake does not support sequences` |
| DuckLake 变体（id 不由库生成 + 建表语句无 PK/UNIQUE/FK） | **成功**，四张表 `users/projects/batches/file_versions` 落在 `dlk.main`；再跑一次仍是幂等（`init_db` 每次启动都跑） |

变体的做法不是重写模型：ORM 那边 `id` 仍是主键（不然 mapper 起不来），只是**另外建一份建表用的元数据副本**，
把 PK/UNIQUE/FK 从副本里摘掉，`init_db` 用副本 `create_all`。

## 必须改哪几处（继续走 DuckLake 的话）

| 位置 | 改什么 | 对 sqlite / seekdb 的影响 |
| --- | --- | --- |
| `server/models.py` | 第 3 个分支：DuckLake 的 id 不自动生成、不建 PK/UNIQUE/FK | 无（走原分支） |
| `server/db.py` | `ducklake` 分支：`ATTACH IF NOT EXISTS 'ducklake:sqlite:…'`、catalog 开 WAL、`SET search_path`、建表走元数据副本 | 无 |
| `server/handler.py` | 唯一性 409 改成应用层 `SELECT` 校验（DuckLake 分支）；现有 FK 校验已够用 | 无 |
| 新增一个小模块 | 辅助 SQLite 计数器（id 分配），或等价的应用层分配器 | 无 |

工作量估：models/create_all 分支 0.5 天 + id 分配 0.5 天 + 应用层唯一校验与 409 0.5–1 天 + db.py attach/WAL 0.5 天
≈ **2 人天以内**（D2 的并发重试另算），**但换来的是比 sqlite/seekdb 更弱的保证**。

## 顺带实测到的三个坑（给 D2）

1. **二次 ATTACH 会炸**：duckdb-engine 的连接池会给同一个 DuckDB 文件再开底层连接，那个连接已经挂过 `dlk`，
   无条件 `ATTACH` 报 `Binder Error: … database with name "dlk" already exists` → 用 `ATTACH IF NOT EXISTS`。
2. **catalog 必须开 WAL**：默认 delete journal 时，池里第二条连接读 snapshot 会被写者挡住 ——
   `Failed to query most recent snapshot for DuckLake: database is locked`（与 D6 的结论一致，这里是应用侧复现）。
3. **DuckLake 不支持 `UPDATE … RETURNING`**：计数表那类"写一行拿一个号"的写法要用两条语句，或者干脆放辅助 SQLite。

## D1 要不要返工

**要，返工范围如上表。** D1（PR #23）是按"裸 DuckDB + `Sequence`"写的，而 D-1 已定 DuckLake + SQLite catalog，
那套写法在 DuckLake 上连表都建不出来；`LABFLOW_DB` 分支这层抽象可以留，分支内容要换成
"id 应用层生成 + 不建约束 + 应用层唯一/FK 校验"。

## 给决策的一句话

接受"唯一性/外键只在应用层、无 DB 兜底"，DuckLake 这条路能走（约 2 人天返工，每次写 17–22 ms，一批校验逻辑）；
如果"批次名唯一"这类语义必须是 DB 级硬保证，那 DuckLake 的模型层成本比 09-26 那轮评估更高，
D6 给出的 sqlite 备选（WAL + `busy_timeout`，多进程同时读、模型零改动）反而更省。
这条结论只影响 D1 的实现方式与 D2 的范围，D0/D5/D6 的结论不变。
