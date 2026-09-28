"""D5 实测脚本共用的子进程工具：长驻"应用"进程（Holder）与一次性探测。"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPIKE = Path(__file__).resolve().parent


class Holder:
    """长驻"应用"进程；命令与事件走 stdin/stdout 的 JSON 行。"""

    def __init__(self, kind: str, db: Path, mode: str = "rw", busy_timeout: float = 5.0, begin_sql: str | None = None):
        argv = [
            sys.executable,
            str(SPIKE / "holder.py"),
            "--kind",
            kind,
            "--db",
            str(db),
            "--mode",
            mode,
            "--busy-timeout",
            str(busy_timeout),
        ]
        if begin_sql:
            argv += ["--begin-sql", begin_sql]
        self.argv = argv
        self.proc = subprocess.Popen(
            argv,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.events: list[dict] = []

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line.rstrip())
        self.lines.put(None)

    def wait_event(self, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                event = {"event": "timeout"}
                self.events.append(event)
                return event
            try:
                raw = self.lines.get(timeout=remaining)
            except queue.Empty:
                event = {"event": "timeout"}
                self.events.append(event)
                return event
            if raw is None:
                event = {"event": "eof", "returncode": self.proc.poll()}
                self.events.append(event)
                return event
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                event = {"event": "raw", "line": raw}
            self.events.append(event)
            if event.get("event") != "error":
                return event

    def command(self, cmd: str, timeout: float = 20.0) -> dict:
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()
        return self.wait_event(timeout)

    def stop(self):
        try:
            self.proc.stdin.write("exit\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()
            self.proc.wait(timeout=5)


def run_json(argv: list[str], timeout: float = 120.0) -> list[dict]:
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"event": "raw", "line": line})
    for line in proc.stderr.splitlines():
        if line.strip():
            events.append({"event": "stderr", "line": line.strip()})
    events.append({"event": "exit_code", "returncode": proc.returncode})
    return events
