"""D4 双跑探针：对指定后端跑一遍真实业务流程，把逐接口响应归一化后落盘。

用法（先起好服务）::

    pixi run python spikes/d4/dual_run_probe.py seed  --base-url http://127.0.0.1:9101 --out seed.json
    pixi run python spikes/d4/dual_run_probe.py probe --base-url http://127.0.0.1:9101 --out a.json

seed  造一份"现网"数据（项目/批次/文件 + 回收站各一条），用来当迁移源。
probe 跑真实流程（建项目/批次、上传、下载、删除、恢复、回收站），比对时用。
      id 与状态码原样保留（两后端必须一致），时间戳归一成 <TS> 再比。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys

import requests

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)?")


def _norm(value):
    if isinstance(value, str):
        return TIMESTAMP.sub("<TS>", value)
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        result = {k: _norm(v) for k, v in value.items()}
        # /api/file-config 的 file_fields 直接来自 config 里的 set，顺序随
        # PYTHONHASHSEED 变（同一后端重跑也会变），跟后端无关，排序后再比。
        if isinstance(result.get("file_fields"), dict):
            result["file_fields"] = {k: sorted(v) for k, v in result["file_fields"].items()}
        return result
    return value


def _csv(name):
    filename = f"{name}.csv"
    return filename, f"compound,value\n{name},1\n".encode("utf-8"), "text/csv"


class Client:
    def __init__(self, base_url, out):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.steps = []
        self.out = out

    def record(self, name, method, path, resp):
        entry = {"step": name, "method": method, "path": path, "status": resp.status_code}
        ctype = resp.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            try:
                entry["body"] = _norm(resp.json())
            except ValueError:
                entry["body"] = None
        else:
            entry["body"] = None
            entry["bytes_sha256"] = hashlib.sha256(resp.content).hexdigest()
            entry["bytes_len"] = len(resp.content)
        self.steps.append(entry)
        flag = "  <-- 5xx" if resp.status_code >= 500 else ""
        print(f"{resp.status_code:3d} {method:6s} {path}{flag}")
        return entry

    def call(self, name, method, path, **kw):
        resp = self.s.request(method, self.base + path, timeout=30, **kw)
        return self.record(name, method, path, resp)

    def json_call(self, name, method, path, payload=None):
        resp = self.s.request(method, self.base + path, json=payload, timeout=30)
        return self.record(name, method, path, resp)

    def login(self):
        resp = self.s.post(
            self.base + "/api/login",
            json={"username": "leader", "password": "labflow123"},
            timeout=30,
        )
        assert resp.status_code == 200, f"登录失败：{resp.status_code} {resp.text}"
        self.record("login", "POST", "/api/login", resp)

    def dump(self):
        five_xx = [s for s in self.steps if s["status"] >= 500]
        payload = {"steps": self.steps, "five_xx": five_xx, "ok": not five_xx}
        with open(self.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"\n共 {len(self.steps)} 个调用，5xx {len(five_xx)} 个 → {self.out}")
        return 0 if not five_xx else 1


def seed(base_url, out):
    c = Client(base_url, out)
    c.login()

    def post_project(name):
        c.json_call(f"seed 建项目 {name}", "POST", "/api/projects", {"name": name})
        return c.steps[-1]["body"]["project"]["id"]

    def post_batch(pid, no, name, remark):
        c.json_call(
            f"seed 建批次 {name}", "POST", "/api/batches",
            {"project_id": pid, "batch_no": no, "name": name, "remark": remark},
        )
        return c.steps[-1]["body"]["batch"]["id"]

    def upload(bid, name, file_type):
        fname, body, ctype = _csv(name)
        c.call(
            f"seed 上传 {name}", "POST", f"/api/batches/{bid}/files",
            data={"file_type": file_type},
            files={"file": (fname, body, ctype)},
        )
        return c.steps[-1]["body"]["batch"]["files"][file_type]["latest"]["id"]

    projects = {
        "阿司匹林工艺优化": "B-2026-0",
        "布洛芬晶型研究": "B-2026-1",
        "对乙酰氨基酚中试": "B-2026-2",
        "已搁置项目": "B-2026-3",
    }
    batches, files, pids = [], [], []
    for index, (pname, prefix) in enumerate(projects.items(), start=1):
        pid = post_project(pname)
        pids.append(pid)
        for seq in range(1, 4):
            bid = post_batch(pid, f"{prefix}{index}{seq}", f"{prefix}{index}{seq} 批次{seq}", f"第{seq}轮")
            batches.append(bid)
            if (index + seq) % 2 == 0:
                files.append(upload(bid, f"compound_{index}{seq}", "compound_info"))

    # 回收站各来几条：删文件、删批次、删项目（连批次一起软删）
    for fid in files[:2]:
        c.call(f"seed 删文件 {fid}", "DELETE", f"/api/files/{fid}")
    for bid in batches[3:5]:
        c.call(f"seed 删批次 {bid}", "DELETE", f"/api/batches/{bid}")
    c.call("seed 删项目 已搁置项目", "DELETE", f"/api/projects/{pids[3]}")
    return c.dump()


def probe(base_url, out):
    c = Client(base_url, out)
    c.login()
    c.call("me", "GET", "/api/me")
    c.call("users", "GET", "/api/users")
    c.call("file-config", "GET", "/api/file-config")
    c.call("projects 基线", "GET", "/api/projects")

    c.json_call("建项目", "POST", "/api/projects", {"name": "D4-项目A"})
    pid = c.steps[-1]["body"]["project"]["id"]
    c.call("projects 建后", "GET", "/api/projects")

    c.json_call(
        "建批次", "POST", "/api/batches",
        {"project_id": pid, "batch_no": "D4-B-001", "name": "D4-批次-001", "remark": "双跑比对"},
    )
    bid = c.steps[-1]["body"]["batch"]["id"]

    fname, body, ctype = _csv("d4_probe")
    c.call(
        "上传文件", "POST", f"/api/batches/{bid}/files",
        data={"file_type": "compound_info"},
        files={"file": (fname, body, ctype)},
    )
    fid = c.steps[-1]["body"]["batch"]["files"]["compound_info"]["latest"]["id"]

    c.call("batches 列表", "GET", f"/api/batches?project_id={pid}")
    c.call("下载文件", "GET", f"/api/files/{fid}/download")
    c.json_call("改批次备注", "PATCH", f"/api/batches/{bid}", {"remark": "改过的备注"})

    c.call("删文件", "DELETE", f"/api/files/{fid}")
    c.call("trash 有文件", "GET", "/api/trash")
    c.call("恢复文件", "POST", f"/api/files/{fid}/restore")
    c.call("删批次", "DELETE", f"/api/batches/{bid}")
    c.call("trash 有批次", "GET", "/api/trash")
    c.call("恢复批次", "POST", f"/api/batches/{bid}/restore")
    c.call("删项目", "DELETE", f"/api/projects/{pid}")
    c.call("trash 有项目", "GET", "/api/trash")
    c.call("恢复项目", "POST", f"/api/projects/{pid}/restore")

    # 唯一性（含回收站、区分大小写）
    c.json_call("重名项目 409", "POST", "/api/projects", {"name": "D4-项目A"})
    c.json_call(
        "重名批次 409", "POST", "/api/batches",
        {"project_id": pid, "batch_no": "D4-B-002", "name": "D4-批次-001"},
    )
    c.json_call("大小写不同可共存", "POST", "/api/projects", {"name": "d4-项目a"})
    return c.dump()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("seed", "probe"):
        p = sub.add_parser(name)
        p.add_argument("--base-url", default="http://127.0.0.1:9101")
        p.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    return {"seed": seed, "probe": probe}[args.cmd](args.base_url, args.out)


if __name__ == "__main__":
    sys.exit(main())
