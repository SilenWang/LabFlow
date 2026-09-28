"""D0 预研：sqlite / duckdb 两后端各自 ``create_all`` + 取回自增 id。

结论先行：自增主键**没有**一个能覆盖两后端的写法，只能按后端分支。

- sqlite：``Column(Integer, primary_key=True, autoincrement=True)``
  （sqlite ``INTEGER PRIMARY KEY``）。
- duckdb：``Column(Integer, Sequence("<table>_id_seq"), primary_key=True,
  server_default=seq.next_value())``——duckdb 既没有 ``SERIAL``，也没有
  ``GENERATED ... AS IDENTITY``，只有 ``DEFAULT nextval('seq')``。

脚本对每个后端把"分支写法"和"统一写法"都真跑一遍（``create_all`` + 插入 + 读回 id），
形成对照矩阵；只有分支写法能两后端全部可行。退出码 0 = 矩阵与预期一致。

顺带留一条给 D3（迁移脚本）的实测：显式插入 ``id=7`` 之后，duckdb 的序列不跟随，
下一行自增拿到 ``id=3``——序列必须显式 restart 到 ``max(id)+1``。

用法::

    pixi run pk-spike
    pixi run pk-spike -- --only duckdb
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import traceback
from pathlib import Path

# 脚本在 spikes/ 下执行，`server` 包在仓库根，补一下 import 路径。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import Column, ForeignKey, Integer, MetaData, Sequence, String, create_engine, text
from sqlalchemy.orm import Session, declarative_base

BACKENDS = ("sqlite", "duckdb")

# 每个后端上哪种写法才是对的；脚本据此断言矩阵。
BRANCHED_FORM = {"sqlite": "autoincrement", "duckdb": "sequence"}
FORMS = ("autoincrement", "sequence")


def _pk_column(form, table):
    if form == "sequence":
        seq = Sequence(f"{table}_id_seq")
        return Column(Integer, seq, primary_key=True, server_default=seq.next_value())
    return Column(Integer, primary_key=True, autoincrement=True)


def _model(form):
    """按 form 造一套最小模型：父表 projects + 带外键的子表 batches（对齐真实形状）。"""
    base = declarative_base(metadata=MetaData())

    class Project(base):
        __tablename__ = "projects"
        id = _pk_column(form, "projects")
        name = Column(String(80), nullable=False, unique=True)

    class Batch(base):
        __tablename__ = "batches"
        id = _pk_column(form, "batches")
        project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
        name = Column(String(120), nullable=False, unique=True)

    return base, Project, Batch


def _engine(backend, tmp):
    scheme = "duckdb" if backend == "duckdb" else "sqlite"
    return create_engine(f"{scheme}:///{tmp / ('labflow.db' if scheme == 'sqlite' else 'labflow.duckdb')}")


def _cleanup(backend, engine):
    engine.dispose()


def attempt(backend, form, tmp):
    """建表 → 插两条 → 子表插一条 → 显式插 id=7 → 再插一条，全部真跑。"""
    engine = _engine(backend, tmp)
    base, Project, Batch = _model(form)
    try:
        base.metadata.create_all(engine)
        with Session(engine) as s:
            p1, p2 = Project(name="P1"), Project(name="P2")
            s.add_all([p1, p2])
            s.commit()
            auto_ids = [p1.id, p2.id]

            b1 = Batch(project_id=p1.id, name="B1")
            s.add(b1)
            s.commit()
            child_id = b1.id

            # 迁移脚本会显式带 id 写入；这里看序列会不会因此停住。
            s.add(Project(id=7, name="P7"))
            s.commit()
            p_next = Project(name="P-after-explicit-7")
            s.add(p_next)
            s.commit()
            after_explicit = p_next.id

        with engine.connect() as conn:
            stored = sorted(r[0] for r in conn.execute(text("SELECT id FROM projects")))
        return {
            "ok": True,
            "auto_ids": auto_ids,
            "child_id": child_id,
            "after_explicit": after_explicit,
            "stored": stored,
        }
    except Exception as exc:  # noqa: BLE001 - 预研要把异常原文留档
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}".splitlines()[0]}
    finally:
        _cleanup(backend, engine)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", choices=BACKENDS, action="append", help="只跑指定后端（可重复）")
    args = parser.parse_args(argv)

    wanted = args.only or list(BACKENDS)
    ok = True
    matrix = {}

    for backend in wanted:
        print(f"\n=== {backend}（分支写法：{BRANCHED_FORM[backend]}）===")
        for form in FORMS:
            with tempfile.TemporaryDirectory(prefix=f"pk_spike_{backend}_{form}_") as d:
                try:
                    got = attempt(backend, form, Path(d))
                except Exception:  # noqa: BLE001 - 连建引擎都失败也要留档
                    traceback.print_exc()
                    got = {"ok": False, "error": "建引擎/建库失败"}
            matrix[(backend, form)] = got
            tag = "分支写法" if BRANCHED_FORM[backend] == form else "统一写法"

            if not got["ok"]:
                print(f"  [{tag}] {form:14s} FAIL -> {got['error']}")
                continue

            print(
                f"  [{tag}] {form:14s} ok   前两条 id={got['auto_ids']} "
                f"子表 id={got['child_id']}  显式插 id=7 后下一行 id={got['after_explicit']}"
            )
            if form == "sequence" and backend == "duckdb":
                print(f"           └ 落库 id={got['stored']}：序列不跟随显式 id，D3 必须 restart")

    print("\n对照矩阵")
    print(f"  {'后端':10s} {'autoincrement=True':>20s} {'Sequence+server_default':>26s}")
    for backend in wanted:
        cells = ["ok" if matrix[(backend, f)]["ok"] else "err" for f in FORMS]
        print(f"  {backend:10s} {cells[0]:>20s} {cells[1]:>26s}")

    for backend in wanted:
        for form in FORMS:
            got = matrix[(backend, form)]
            expect_ok = BRANCHED_FORM[backend] == form
            if got["ok"] != expect_ok:
                ok = False
                print(f"  FAIL {backend}/{form}: 期望 {'ok' if expect_ok else 'err'}，实测 {'ok' if got['ok'] else 'err'}")
            if got["ok"] and (got["auto_ids"] != [1, 2] or got["child_id"] != 1):
                ok = False
                print(f"  FAIL {backend}/{form}: 自增语义不符 {got}")

    print(
        "\n结论:",
        "两后端均可 create_all 并取回自增 id，但主键定义必须按后端分支"
        if ok
        else "矩阵与预期不符，见上方 FAIL",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
