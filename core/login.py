"""独立的扫码会话：只向官方登录接口请求，Cookie 不进入日志或消息。"""

from __future__ import annotations

import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie

from .client import XhhClient, XhhError


class QrLoginClient(XhhClient):
    def __init__(self, timeout: float = 20):
        super().__init__("", timeout=timeout)
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._login_ids = ()

    async def ticket(self) -> dict:
        path = "/account/get_qrcode_url/"
        result = self._unwrap(await self.request("GET", path), path)
        url = result.get("qr_url")
        if not isinstance(url, str):
            raise XhhError("登录接口没有返回二维码地址")
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        params = {key: query[key][0] for key in ("qr", "qr_ticket", "app", "device") if key in query}
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
        state = result.get("error")
        if state == "wait":
            return "wait"
        if state in ("ready", "scanned"):
            return "scanned"
        if state == "ok":
            self._login_ids = tuple(result.get(key) for key in ("heybox_id", "user_heybox_id", "heyboxid"))
            return "ok"
        if state in ("timeout", "expired"):
            return "expired"
        if state in ("cancel", "canceled", "denied"):
            return "canceled"
        raise XhhError("登录接口返回了无法识别的扫码状态")

    def login_cookie(self) -> str:
        self._jar.clear_expired_cookies()
        cookies = {
            cookie.name: cookie.value for cookie in self._jar
            if cookie.domain.lstrip(".") in {"xiaoheihe.cn", "api.xiaoheihe.cn", "www.xiaoheihe.cn"}
        }
        if not cookies.get("user_pkey") and cookies.get("pkey"):
            cookies["user_pkey"] = cookies["pkey"]
        if not cookies.get("user_pkey") or not cookies.get("x_xhh_tokenid"):
            raise XhhError("登录响应缺少 user_pkey 或 x_xhh_tokenid，原登录态未替换")
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    def login_heybox_id(self) -> str:
        cookies = SimpleCookie(self.login_cookie())
        values = (*self._login_ids, *(cookies[key].value for key in ("heybox_id", "user_heybox_id") if key in cookies))
        ids = {str(value).strip() for value in values if value is not None and value != ""}
        if (len(ids) != 1 or any(not uid.isascii() or not uid.isdigit() or int(uid) <= 0 for uid in ids)):
            raise XhhError("扫码会话未返回唯一有效的账号 ID，原登录态未替换")
        return ids.pop()
