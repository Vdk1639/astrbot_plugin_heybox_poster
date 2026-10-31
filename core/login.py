"""独立的扫码会话：只向官方登录接口请求，Cookie 不进入日志或消息。"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie

from .transport import XhhError, XhhTransport

ID_KEYS = ("uid", "heybox_id", "user_heybox_id", "heyboxid", "user_id", "userid", "id")


class QrLoginClient(XhhTransport):
    def __init__(self, timeout: float = 20):
        super().__init__("", timeout=timeout)
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self.device_id = secrets.token_hex(16)
        self._result = {}
        self.nickname = ""
        self._token_id = ""

    def _build_url(self, path: str, params: dict | None, write: bool) -> str:
        # 扫码属于网页登录；app=heybox 会生成不同客户端的二维码会话。
        return super()._build_url(path, {"app": "web", **(params or {})}, write)

    async def ticket(self) -> dict:
        path = "/account/get_qrcode_url/"
        result = self._unwrap(await self.request("GET", path), path)
        url = result.get("qr_url")
        if not isinstance(url, str):
            raise XhhError("登录接口没有返回二维码地址")
        parsed = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        if (parsed.scheme != "https" or parsed.netloc != "api.xiaoheihe.cn"
                or parsed.path != "/account/qr_login/"
                or not (params.get("qr") or params.get("qr_ticket"))):
            raise XhhError("登录接口返回的二维码地址无效")
        try:
            expire = int(result.get("expire", 120))
        except (TypeError, ValueError):
            raise XhhError("二维码有效期无效") from None
        if expire <= 0:
            raise XhhError("二维码已过期")
        return {"url": url, "params": params, "expire": min(expire, 120)}

    async def poll(self, params: dict) -> str:
        path = "/account/qr_state/"
        result = self._unwrap(await self.request("GET", path, params=params), path)
        states = {
            "0": "wait", "wait": "wait", "waiting": "wait", "waiting_scan": "wait",
            "1": "scanned", "ready": "scanned", "scanned": "scanned", "confirm": "scanned",
            "waiting_confirm": "scanned", "2": "ok", "ok": "ok", "success": "ok", "confirmed": "ok",
            "3": "expired", "timeout": "expired", "expired": "expired",
            "cancel": "canceled", "canceled": "canceled", "denied": "canceled", "refuse": "canceled",
            "-1": "failed", "failed": "failed",
        }
        marker = next((result[key] for key in ("state", "status", "qr_state")
                       if result.get(key) is not None and result[key] != ""), None)
        state = states.get(str(marker).strip().lower())
        if state is None:
            state = states.get(str(result.get("error") or result.get("err") or "").strip().lower(), "wait")
        if state == "ok":
            self._result = result
            self.nickname = next((value for item in self._containers() for key in ("nickname", "username", "name")
                                  if isinstance(value := item.get(key), str) and value), "")
        return state

    def _containers(self):
        return [self._result, *(item for key in ("user", "account", "profile", "account_detail")
                                if isinstance(item := self._result.get(key), dict))]

    def _session_cookies(self):
        self._jar.clear_expired_cookies()
        return {
            cookie.name: cookie.value for cookie in self._jar
            if cookie.domain.lstrip(".") in {"xiaoheihe.cn", "api.xiaoheihe.cn", "www.xiaoheihe.cn"}
        }

    def login_cookie(self) -> str:
        cookies = self._session_cookies()
        for item in self._containers():
            if isinstance(item.get("cookies"), dict):
                cookies.update(item["cookies"])
            for key in ("user_pkey", "pkey", "x_xhh_tokenid"):
                if item.get(key) and not cookies.get(key):
                    cookies[key] = item[key]
            if not cookies.get("user_pkey"):
                cookies["user_pkey"] = item.get("pkey") or item.get("key") or ""
        if not cookies.get("user_pkey") and cookies.get("pkey"):
            cookies["user_pkey"] = cookies["pkey"]
        if not cookies.get("user_pkey"):
            raise XhhError("登录响应缺少 user_pkey，原登录态未替换")
        if not cookies.get("x_xhh_tokenid"):
            if not self._token_id:
                # 适配器 MIT 实现的客户端标识公式；此值不是服务端登录凭据。
                parts = (str(int(time.time())), *(secrets.token_hex(16) for _ in range(3)))
                raw = b"".join(hashlib.md5(part.encode(), usedforsecurity=False).digest() for part in parts)
                self._token_id = base64.b64encode(raw + b"\x00").decode("ascii")
            cookies["x_xhh_tokenid"] = self._token_id
        cookies.setdefault("device_id", self.device_id)
        parsed = SimpleCookie()
        for name, value in cookies.items():
            if (not isinstance(name, str) or not isinstance(value, str)
                    or any(c in name + value for c in "\r\n\x00")):
                raise XhhError("登录 Cookie 格式无效，原登录态未替换")
            parsed[name] = value
        return parsed.output(header="", sep=";").strip()

    def login_heybox_id(self) -> str:
        sources = [self._session_cookies(), *self._containers()]
        sources.extend(item["cookies"] for item in self._containers() if isinstance(item.get("cookies"), dict))
        values = [item.get(key) for item in sources for key in ID_KEYS]
        ids = {str(value).strip() for value in values if value is not None and value != ""}
        if (len(ids) != 1 or any(not uid.isascii() or not uid.isdigit() or int(uid) <= 0 for uid in ids)):
            raise XhhError("扫码会话未返回唯一有效的账号 ID，原登录态未替换")
        return ids.pop()
