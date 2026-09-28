"""LabFlow HTTP API 的只读客户端。

只做三件事：登录拿会话 cookie、GET 拿 JSON、把失败翻译成人能看懂的报错。
所有请求都带超时——应用没跑时宁可立刻报错，也不让 MCP 工具挂死。
"""

from __future__ import annotations

import threading
from typing import Any

import requests

from .config import API_LOGIN, Settings


class LabFlowError(RuntimeError):
    """调用 LabFlow HTTP API 失败（网络、鉴权、业务错误都收敛到这里）。"""


def _short_reason(exc: Exception) -> str:
    text = str(exc)
    text = text.split("\n")[0].strip()
    return text or exc.__class__.__name__


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    body = (response.text or "").strip().replace("\n", " ")
    return body[:200] if body else f"HTTP {response.status_code}"


class LabFlowClient:
    """线程安全（MCP server 可能并发处理工具调用）。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._session = requests.Session()
        self._logged_in = False
        self._lock = threading.Lock()

    # --- 内部 ---------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.settings.api_url}{path}"

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = self._url(path)
        try:
            return self._session.request(
                method, url, timeout=self.settings.timeout, **kwargs
            )
        except requests.exceptions.Timeout as exc:
            raise LabFlowError(
                f"访问 LabFlow 超时（{self.settings.timeout:g}s）：{url}。"
                "应用可能未启动或正在重启，请确认后重试。"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise LabFlowError(
                f"无法连接 LabFlow 应用：{url}（{_short_reason(exc)}）。"
                "请确认应用已启动（pixi run serve）、"
                "且 LABFLOW_API_URL 指向正确地址。"
            ) from exc

    def _require_credentials(self) -> None:
        if not self.settings.username or not self.settings.password:
            raise LabFlowError(
                "未配置 LabFlow 账号：请给 MCP server 进程设置环境变量 "
                "LABFLOW_MCP_USERNAME 与 LABFLOW_MCP_PASSWORD。"
            )

    def login(self) -> None:
        """登录并缓存会话 cookie。成功后 requests.Session 自动带上 Cookie。"""
        self._require_credentials()
        response = self._request(
            "POST",
            API_LOGIN,
            json={
                "username": self.settings.username,
                "password": self.settings.password,
            },
        )
        if response.status_code != 200:
            raise LabFlowError(
                f"登录 LabFlow 失败（HTTP {response.status_code}）：{_error_message(response)}"
            )
        self._logged_in = True

    # --- 对外 ---------------------------------------------------------------

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET 一个 JSON 端点；401 时自动重新登录一次再重试。"""
        with self._lock:
            if not self._logged_in:
                self.login()

        response = self._request("GET", path, params=params)

        if response.status_code == 401:
            # cookie 过期或应用重启换了密钥：重登一次，仍失败才报错。
            with self._lock:
                self.login()
            response = self._request("GET", path, params=params)

        if response.status_code >= 400:
            raise LabFlowError(
                f"LabFlow 接口 {path} 返回 HTTP {response.status_code}：{_error_message(response)}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise LabFlowError(
                f"LabFlow 接口 {path} 返回的不是 JSON（HTTP {response.status_code}）。"
                "请确认 LABFLOW_API_URL 指向的是 LabFlow 应用而不是别的服务。"
            ) from exc

    def get_stream(self, path: str, params: dict[str, Any] | None = None) -> requests.Response:
        """GET 一个二进制端点（文件下载），返回已连上的流式响应。

        调用方负责关闭；这里只保证非 2xx 也给出清晰报错。
        """
        with self._lock:
            if not self._logged_in:
                self.login()

        response = self._request("GET", path, params=params, stream=True)

        if response.status_code == 401:
            response.close()
            with self._lock:
                self.login()
            response = self._request("GET", path, params=params, stream=True)

        if response.status_code >= 400:
            message = _error_message(response)
            response.close()
            raise LabFlowError(
                f"LabFlow 接口 {path} 返回 HTTP {response.status_code}：{message}"
            )
        return response

    def health(self) -> dict[str, Any]:
        """探活：能不能连上 + 能不能登录 + 当前身份。"""
        try:
            me = self.get_json("/api/me")
        except LabFlowError as exc:
            return {"ok": False, "api_url": self.settings.api_url, "error": str(exc)}
        return {
            "ok": True,
            "api_url": self.settings.api_url,
            "username": self.settings.username,
            "user": (me or {}).get("user"),
        }
