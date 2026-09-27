#!/usr/bin/env python
"""用官方本地包 mcp-server-motherduck 真跑一次 stdio 会话（VYB-419 / D5 实测用）。

  pixi run python spikes/duckdb_lock/mcp_probe.py --db-path <file> [--read-write] [--ephemeral]
  pixi run python spikes/duckdb_lock/mcp_probe.py --db-path :memory:

流程：起进程 → initialize → tools/list → tools/call query。
全程不需要 MotherDuck token（走本地文件/内存库，不联网）。
输出每行一个 JSON 事件；退出码 0 = 整条链路成功。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def log(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


class StdioClient:
    def __init__(self, argv):
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.replies: queue.Queue = queue.Queue()
        self.stderr_lines: list[str] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self.replies.put(line)
        self.replies.put(None)

    def _read_stderr(self):
        for line in self.proc.stderr:
            text = line.rstrip()
            # 启动横幅（box drawing / 广告）不算证据，滤掉
            if text.strip() and not any(ch in text for ch in "│╰╭╮╯🚀"):
                self.stderr_lines.append(text)

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def wait_for(self, request_id: int, timeout: float):
        """读到 id 匹配的响应；中间的通知等其他消息按 notice 返回。"""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"timeout": True}
            try:
                raw = self.replies.get(timeout=remaining)
            except queue.Empty:
                return {"timeout": True}
            if raw is None:
                return {"exited": True, "returncode": self.proc.poll()}
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == request_id:
                return msg

    def shutdown(self):
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def summarize(msg: dict) -> dict:
    if "timeout" in msg:
        return {"timeout": True}
    if "exited" in msg:
        return {"process_exited": True, "returncode": msg.get("returncode")}
    if "error" in msg:
        return {"error": msg["error"]}
    result = msg.get("result")
    if isinstance(result, dict) and "serverInfo" in result:
        info = result["serverInfo"]
        return {
            "protocolVersion": result.get("protocolVersion"),
            "server": f"{info.get('name')} {info.get('version')}",
        }
    if isinstance(result, dict) and "tools" in result:
        return {
            "tools": [
                {
                    "name": t.get("name"),
                    "readOnlyHint": (t.get("annotations") or {}).get("readOnlyHint"),
                }
                for t in result["tools"]
            ]
        }
    if isinstance(result, dict) and "content" in result:
        texts = [c.get("text", "") for c in result["content"] if isinstance(c, dict)]
        return {"result_preview": " | ".join(texts)[:500], "isError": result.get("isError", False)}
    return {"result_preview": json.dumps(result, ensure_ascii=False)[:400]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--read-write", action="store_true")
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument("--no-ephemeral", action="store_true", help="传 --no-ephemeral-connections（常驻读连接）")
    parser.add_argument("--sql", default="SELECT count(*) AS n FROM batches")
    parser.add_argument("--tool", default="auto", help="工具名；auto = 在 tools/list 里自动挑")
    parser.add_argument("--label", default="")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--post-probe-db", default=None, help="查询结束后、MCP 进程仍存活时，用另一个进程试开这个文件")
    parser.add_argument("--fd-check", default=None, help="查询结束后检查 MCP 进程是否还握着这个库文件的 fd")
    parser.add_argument("--hold-ms", type=int, default=0, help="查询结束后保持进程存活的时间")
    parser.add_argument("--queries", type=int, default=1, help="同一会话里连续发多少次 execute_query")
    args = parser.parse_args()

    binary = shutil.which("mcp-server-motherduck")
    if binary is None:
        log("fatal", message="mcp-server-motherduck 不在 PATH（pixi run 下应可用）")
        return 2

    argv = [binary, "--db-path", args.db_path]
    if args.read_write:
        argv.append("--read-write")
    if args.ephemeral:
        argv.append("--ephemeral-connections")
    if args.no_ephemeral:
        argv.append("--no-ephemeral-connections")

    log(
        "mcp_start",
        label=args.label,
        argv=argv,
        db_path=args.db_path,
        read_write=args.read_write,
        ephemeral=args.ephemeral,
    )

    client = StdioClient(argv)
    started = time.perf_counter()
    ok = True
    try:
        client.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "d5-probe", "version": "0"},
                },
            }
        )
        reply = client.wait_for(1, args.timeout)
        log("mcp_initialize", ms=round((time.perf_counter() - started) * 1000, 1), **summarize(reply))
        if "timeout" in reply or "exited" in reply or "error" in reply:
            ok = False
            return_early = True
        else:
            return_early = False

        if not return_early:
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

            t = time.perf_counter()
            client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            reply = client.wait_for(2, args.timeout)
            tools = []
            if isinstance(reply.get("result"), dict):
                tools = [t_.get("name") for t_ in reply["result"].get("tools", [])]
            log("mcp_tools_list", ms=round((time.perf_counter() - t) * 1000, 1), **summarize(reply))
            tool_name = args.tool if args.tool != "auto" else next(
                (name for name in ("execute_query", "query") if name in tools), tools[0] if tools else ""
            )
            log("mcp_tool_chosen", tool=tool_name)
            if not tool_name:
                ok = False

            session_ok = 0
            for index in range(1, args.queries + 1):
                t = time.perf_counter()
                client.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 2 + index,
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": {"sql": args.sql}},
                    }
                )
                reply = client.wait_for(2 + index, args.timeout)
                call_ok = not ("timeout" in reply or "exited" in reply or "error" in reply)
                if call_ok and isinstance(reply.get("result"), dict) and reply["result"].get("isError"):
                    call_ok = False
                session_ok += int(call_ok)
                log(
                    "mcp_tool_call_query",
                    query=index,
                    of=args.queries,
                    ok=call_ok,
                    ms=round((time.perf_counter() - t) * 1000, 1),
                    **summarize(reply),
                )
            log("mcp_session_summary", ok_calls=session_ok, total_calls=args.queries)
            if session_ok != args.queries:
                ok = False

            if args.fd_check:
                db_name = os.path.basename(args.fd_check)
                matches = []
                fd_dir = Path(f"/proc/{client.proc.pid}/fd")
                try:
                    for fd in fd_dir.iterdir():
                        try:
                            target = os.readlink(fd)
                        except OSError:
                            continue
                        if db_name in target:
                            matches.append(f"{fd.name} -> {target}")
                except FileNotFoundError:
                    matches = ["<no /proc entry>"]
                log("mcp_fd_check", db=args.fd_check, fd_open=bool(matches), matches=matches)

            if args.hold_ms:
                time.sleep(args.hold_ms / 1000)

            if args.post_probe_db:
                probe = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve().parent / "probe_duckdb.py"),
                        "--db",
                        args.post_probe_db,
                        "--mode",
                        "rw",
                        "--sql",
                        "INSERT INTO projects VALUES (97, 'z', 1, 'ts', NULL)",
                        "--label",
                        "MCP存活期间/另一个进程试开",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                for line in probe.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") == "probe_attempt":
                        payload = {k: v for k, v in event.items() if k != "event"}
                        log("mcp_post_probe", **payload)
    finally:
        client.shutdown()
        log(
            "mcp_exit",
            label=args.label,
            returncode=client.proc.returncode,
            stderr_tail=client.stderr_lines[-8:],
        )

    log("mcp_done", label=args.label, ok=ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
