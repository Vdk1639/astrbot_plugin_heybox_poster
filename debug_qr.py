"""本机最小扫码验证：python debug_qr.py；不保存登录凭据，不依赖 AstrBot。"""

import argparse
import asyncio
import hashlib
import json
import secrets
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path

from core.client import XhhClient
from core.login import ID_KEYS, QrLoginClient
from core.sign import signed_params


MARKERS = set("ok wait waiting ready scanned confirm waiting_confirm success confirmed timeout expired cancel canceled denied refuse failed login relogin show_captcha 0 1 2 3 -1".split())
COOKIE_KEYS = ("pkey", "user_pkey", "x_xhh_tokenid", "device_id", *ID_KEYS)
REFERENCE_PARAMS = {
    "os_type": "web", "app": "web", "client_type": "web", "version": "999.0.4", "web_version": "2.5",
    "x_client_type": "web", "x_app": "heybox_website", "x_os_type": "Windows", "device_info": "Chrome", "_notip": "true",
}


def marker(value):
    value = str(value).strip().lower() if isinstance(value, (str, int)) else ""
    return value if value in MARKERS else "<other>" if value else "<empty>"


def summarize(payload, cookies):
    """只输出固定字段与存在性；未知标记和任意服务端文案不直接输出。"""
    body = payload.get("result", payload.get("data", {}))
    body = body if isinstance(body, dict) else {}
    sources = [body, *(body[key] for key in ("user", "account", "profile", "account_detail")
                       if isinstance(body.get(key), dict))]
    sources += [item["cookies"] for item in sources if isinstance(item.get("cookies"), dict)]
    sources.append(cookies)
    return {
        "status": marker(payload.get("status")),
        "markers": {key: marker(body[key]) for key in ("error", "err", "state", "status", "qr_state") if key in body},
        "body_fields": len(body),
        "has_pkey": any(item.get(key) for item in sources for key in ("pkey", "user_pkey", "key")),
        "has_id": any(item.get(key) for item in sources for key in ID_KEYS),
        "has_server_token": any(item.get("x_xhh_tokenid") for item in sources),
        "cookie_names": [key for key in COOKIE_KEYS if key in cookies],
        "other_cookie_count": len(set(cookies) - set(COOKIE_KEYS)),
    }


class HttpObserver(urllib.request.BaseHandler):
    handler_order = 100

    def __init__(self, emit, reference=False):
        self.emit = emit
        self.reference = reference

    def http_request(self, request):
        if self.reference:
            request.remove_header("Origin")
            request.add_header("User-Agent", "AstrBot-Xiaoheihe-Adapter/1.1.3")
            request.add_header("Accept", "application/json")
        return request

    https_request = http_request

    def http_response(self, request, response):
        self.emit("http", path=urllib.parse.urlsplit(request.full_url).path,
                  code=response.code, set_cookie_count=len(response.headers.get_all("Set-Cookie", [])),
                  redirect=bool(response.headers.get("Location")))
        return response

    https_response = http_response


class DiagnosticClient(QrLoginClient):
    def __init__(self, emit, reference=False):
        super().__init__()
        self.emit = emit
        self.reference = reference
        self._opener.add_handler(HttpObserver(emit, reference))

    def _build_url(self, path, params, write):
        if not self.reference:
            return super()._build_url(path, params, write)
        # 仅诊断对照：复现参考适配器的 GET 参数，不修改插件的请求配置。
        query = {**REFERENCE_PARAMS, **(params or {})}
        query.setdefault("device_id", self.device_id)
        query.update(signed_params(path))
        return f"{self.base}{path}?{urllib.parse.urlencode(query)}"

    def _request_sync(self, method, url, form):
        path = urllib.parse.urlsplit(url).path
        started = time.monotonic()
        try:
            payload = super()._request_sync(method, url, form)
        except Exception as exc:
            self.emit("request_error", path=path, error_type=type(exc).__name__)
            raise
        self.emit("response", path=path, elapsed_ms=round((time.monotonic() - started) * 1000),
                  **summarize(payload, self._session_cookies()))
        return payload


async def diagnose(directory, emit, reference=False):
    client = DiagnosticClient(emit, reference)
    stage = "ticket"
    try:
        import qrcode

        async with asyncio.timeout(20):
            ticket = await client.ticket()
        qr_path = directory / "qr.png"
        qrcode.make(ticket["url"]).save(qr_path)
        emit("qr_ready", image=str(qr_path), ttl=ticket["expire"],
             qr_app=ticket["params"].get("app") if ticket["params"].get("app") in {"web", "heybox"} else "<other>",
             session=hashlib.sha256(ticket["url"].encode()).hexdigest()[:12],
             query_keys=sorted(key for key in ticket["params"] if key in {
                 "qr", "qr_ticket", "app", "device", "device_id", "request_id", "_time", "nonce", "hkey"}),
             other_query_count=len(set(ticket["params"]) - {
                 "qr", "qr_ticket", "app", "device", "device_id", "request_id", "_time", "nonce", "hkey"}))
        print("请用小黑盒 App 扫描本次 qr.png 并确认；确认后告知聊天中的助手。", flush=True)
        stage = "poll"
        deadline = time.monotonic() + ticket["expire"]
        failures = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(min(2 ** min(failures, 4), 10) if failures else 3)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                async with asyncio.timeout(remaining):
                    state = await client.poll(ticket["params"])
                failures = 0
            except Exception as exc:
                failures += 1
                emit("poll_retry", error_type=type(exc).__name__, retries=failures)
                continue
            emit("poll", state=state, remaining_seconds=round(deadline - time.monotonic()))
            if state in ("expired", "canceled", "failed"):
                emit("result", outcome="server_" + state)
                return 2
            if state != "ok":
                continue
            stage = "extract_cookie"
            cookie = client.login_cookie()
            stage = "extract_identity"
            uid = client.login_heybox_id()
            emit("credentials_valid", has_pkey=bool(SimpleCookie(cookie).get("user_pkey")),
                 has_token=bool(SimpleCookie(cookie).get("x_xhh_tokenid")), has_id=bool(uid))
            stage = "verify"
            async with XhhClient(cookie, heybox_id=uid) as authenticated:
                for path in ("/account/restore_login", "/bbs/app/api/user/permission"):
                    try:
                        payload = await authenticated.request("GET", path)
                        emit("verification", path=path, **summarize(payload, {}))
                    except Exception as exc:
                        emit("verification_error", path=path, error_type=type(exc).__name__)
            emit("result", outcome="server_confirmed_credentials_extracted", credentials_saved=False)
            return 0
        emit("result", outcome="deadline_without_server_success")
        return 2
    except Exception as exc:
        emit("result", outcome="failed", stage=stage, error_type=type(exc).__name__)
        return 1
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", action="store_true", help="使用参考适配器的 GET 参数与请求头做对照")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent / ".qr-debug" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3))
    directory.mkdir(parents=True)
    log = directory / "diagnostic.jsonl"

    def emit(event, **fields):
        line = json.dumps({"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                           "event": event, **fields}, ensure_ascii=False)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        print(line, flush=True)

    emit("start", python=sys.version.split()[0], log=str(log), profile="reference" if args.reference else "plugin",
         proxy_schemes=[key for key in urllib.request.getproxies() if key in {"http", "https", "all"}])
    try:
        return asyncio.run(diagnose(directory, emit, args.reference))
    except KeyboardInterrupt:
        emit("result", outcome="canceled_locally")
        return 130


if __name__ == "__main__":
    sys.exit(main())
