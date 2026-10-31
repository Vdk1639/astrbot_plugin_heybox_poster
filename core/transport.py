"""小黑盒共享请求层：签名、HTTP 与响应校验，不包含发帖或扫码流程。"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from http.cookies import SimpleCookie
from typing import Any, Self

from .sign import signed_params, workshop_rnd

API_BASE = "https://api.xiaoheihe.cn"

#: 网页端全站公共参数
BASE_PARAMS = {
    "app": "heybox",
    "os_type": "web",
    "x_app": "heybox_website",
    "x_client_type": "web",
    "x_os_type": "Windows",
    "x_client_version": "",
    "client_type": "web",
    "web_version": "3.0",
    "version": "999.0.4",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

_ERROR_STATUS = {"failed", "login", "relogin", "expired", "frontend_failed"}


class XhhError(Exception):
    """业务失败 / 网络失败。"""

    def __init__(self, message: str, *, status: str | None = None,
                 path: str | None = None, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.path = path
        self.payload = payload


class CaptchaRequired(XhhError):
    """命中风控验证码：``x_xhh_tokenid`` 缺失或过期。"""


class RateLimited(XhhError):
    """触发发帖限流（错误码 ``10006``），调用方应提示稍后再试，不自动重试。"""


class NetworkError(XhhError):
    """网络或响应异常，写操作结果未知，需要核实。"""


class XhhTransport:
    """一个会话一个实例，写请求不自动重试。"""

    def __init__(self, cookie: str, heybox_id: str | int = "",
                 timeout: float = 20.0, base: str = API_BASE):
        self.cookie = (cookie or "").strip()
        self.heybox_id = str(heybox_id or "").strip()
        self.timeout = float(timeout)
        self.base = base.rstrip("/")
        self._opener = None
        device = SimpleCookie(self.cookie).get("device_id")
        self.device_id = device.value if device else ""

    # ------------------------------------------------------------------
    @property
    def logged_in(self) -> bool:
        return "user_pkey=" in self.cookie

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """无连接池，保留接口方便插件 terminate 调用。"""
        return None

    # ------------------------------------------------------------------
    def _build_url(self, path: str, params: dict | None, write: bool) -> str:
        signed = signed_params(path)
        query: dict[str, Any] = dict(BASE_PARAMS)
        if self.device_id:
            query["device_id"] = self.device_id
        query.update({k: v for k, v in (params or {}).items() if v is not None})
        if self.heybox_id:
            query["heybox_id"] = self.heybox_id
        query.update(signed)
        if write:
            query["_rnd"] = workshop_rnd(signed)
        return f"{self.base}{path}?{urllib.parse.urlencode(query, encoding='utf-8')}"

    def _request_sync(self, method: str, url: str, form: dict | None) -> dict:
        headers = {
            "User-Agent": UA,
            "Referer": "https://www.xiaoheihe.cn/",
            "Origin": "https://www.xiaoheihe.cn",
            "Accept": "application/json, text/plain, */*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        body = None
        if (method or "GET").upper() == "POST":
            body = urllib.parse.urlencode(form or {}, encoding="utf-8").encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"

        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        open_url = self._opener.open if self._opener is not None else urllib.request.urlopen
        try:
            try:
                with open_url(req, timeout=self.timeout) as resp:
                    code, raw = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                code, raw = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as exc:
            raise NetworkError(f"请求失败：{type(exc).__name__}: {exc}") from exc

        text = raw.decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise NetworkError(f"响应不是 JSON（HTTP {code}），无法确认结果") from exc
        if not isinstance(payload, dict):
            raise NetworkError(f"响应结构异常（HTTP {code}），无法确认结果")
        if code >= 500:
            raise NetworkError(f"服务端错误 HTTP {code}，无法确认结果", payload=payload)
        if code >= 400:
            self._unwrap(payload, urllib.parse.urlsplit(url).path)
            raise XhhError(f"请求被拒绝 HTTP {code}", payload=payload)
        return payload

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        form: dict | None = None,
        write: bool = False,
    ) -> dict:
        url = self._build_url(path, params, write)
        try:
            return await asyncio.to_thread(self._request_sync, method, url, form)
        except XhhError as exc:
            if exc.path is None:
                exc.path = path
            raise

    # ------------------------------------------------------------------
    def _unwrap(self, payload: dict, path: str) -> dict:
        if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
            raise NetworkError("响应缺少有效业务状态，无法确认结果", path=path)
        status = payload.get("status")
        if status == "show_captcha":
            raise CaptchaRequired(
                "触发风控验证码：cookie 里的 x_xhh_tokenid 缺失或已失效",
                status=status, path=path,
            )
        msg = str(payload.get("msg") or "")
        code = payload.get("code") or payload.get("error_code")
        if "10006" in msg or code in (10006, "10006"):
            raise RateLimited(
                "发帖频率过快，请稍后再试",
                status=status, path=path, payload=payload,
            )
        if status in _ERROR_STATUS:
            hint = "（登录态失效，请重新提供 cookie）" if status in {"login", "relogin", "expired"} else ""
            raise XhhError((msg or f"status={status}") + hint,
                           status=status, path=path, payload=payload)
        if status != "ok":
            raise NetworkError(f"未知业务状态：{status}，无法确认结果",
                               status=status, path=path, payload=payload)
        result = payload.get("result", {})
        if not isinstance(result, dict):
            raise NetworkError("响应 result 结构异常，无法确认结果", path=path)
        return result
