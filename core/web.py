"""账号页面通过 AstrBot Dashboard 的认证桥接访问，不单独监听端口。"""

from __future__ import annotations

import asyncio
import base64
import io
import secrets
import time

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .client import XhhClient
from .login import QrLoginClient


class AccountWebUI:
    def __init__(self, plugin):
        self.plugin = plugin
        self._lock = asyncio.Lock()
        self._task = None
        self._login = {"state": "idle"}
        self._account = {}
        self._closed = False

    def register(self, context):
        for path, handler, method in (
            ("auth/status", self.status, "GET"),
            ("auth/qr", self.qr, "POST"),
            ("auth/cancel", self.cancel, "POST"),
            ("auth/check", self.check, "POST"),
            ("auth/logout", self.logout, "POST"),
        ):
            context.register_web_api(
                f"/astrbot_plugin_heybox_poster/{path}", handler, [method], "小黑盒发帖账号管理"
            )

    @staticmethod
    def _unauthorized():
        if not request.username:
            return error_response("请先登录 AstrBot Dashboard", status_code=401)
        return None

    def _snapshot(self):
        return {
            "login": dict(self._login),
            "account": {
                "has_credentials": bool(self.plugin._cookie()),
                "user_id": str(self.plugin._c("heybox_id", "")),
                **self._account,
            },
        }

    async def status(self):
        if response := self._unauthorized():
            return response
        return json_response(self._snapshot())

    async def qr(self):
        if response := self._unauthorized():
            return response
        async with self._lock:
            if self._closed:
                return error_response("插件正在卸载，请刷新后重试", status_code=503)
            await self._stop()
            client = QrLoginClient(timeout=float(self.plugin._c("request_timeout", 20) or 20))
            self._login = {"state": "requesting"}
            try:
                async with asyncio.timeout(20):
                    ticket = await client.ticket()
                import qrcode

                image = io.BytesIO()
                qrcode.make(ticket["url"]).save(image, format="PNG")
                self._login = {
                    "state": "wait", "expires_at": time.time() + ticket["expire"],
                    "session_id": secrets.token_hex(3), "poll_count": 0,
                    "qr_image": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii"),
                }
                self._task = asyncio.create_task(self._poll(client, ticket))
            except BaseException as exc:
                self._login = {"state": "failed"}
                await client.close()
                if not isinstance(exc, Exception):
                    raise
                logger.warning(f"[heybox_poster] 获取二维码失败：{type(exc).__name__}")
                return error_response("获取二维码失败，请重试或检查依赖及网络", status_code=502)
            return json_response(self._snapshot())

    async def _poll(self, client, ticket):
        stage = "查询扫码状态"
        failures = 0
        try:
            async with asyncio.timeout(ticket["expire"]):
                while True:
                    await asyncio.sleep(min(2 ** min(failures, 4), 10) if failures else 3)
                    try:
                        state = await client.poll(ticket["params"])
                    except Exception as exc:
                        failures += 1
                        self._login.update(retries=failures, retrying=True)
                        logger.warning(f"[heybox_poster] 查询扫码状态暂时失败，第 {failures} 次重试（{type(exc).__name__}）")
                        continue
                    failures = 0
                    self._login.update(retrying=False, retries=0, updated_at=time.time(),
                                       poll_count=self._login.get("poll_count", 0) + 1)
                    if state in ("wait", "scanned"):
                        if state == "scanned" or self._login["state"] != "scanned":
                            self._login["state"] = state
                        continue
                    if state in ("expired", "canceled", "failed"):
                        self._login = {"state": state}
                        return
                    self._login["state"] = "saving"
                    stage = "读取扫码凭据"
                    cookie = client.login_cookie()
                    stage = "读取新账号 ID"
                    heybox_id = client.login_heybox_id()
                    status = {"user_id": heybox_id, "nickname": client.nickname}
                    stage = "保存登录配置"
                    self.plugin._save_login(cookie, status)
                    self._account = {"logged_in": True, "nickname": status.get("nickname", "")}
                    self._login = {"state": "success"}
                    return
        except TimeoutError:
            self._login = {"state": "expired"}
        except asyncio.CancelledError:
            self._login = {"state": "canceled"}
            raise
        except Exception as exc:
            logger.warning(f"[heybox_poster] 扫码登录失败：{stage}（{type(exc).__name__}）")
            self._login = {"state": "failed", "message": f"{stage}失败，原账号保留，请检查配置或重新扫码。"}
        finally:
            await client.close()

    async def _verify_account(self, cookie, heybox_id, *, timeout=20):
        async with XhhClient(cookie, heybox_id=heybox_id, timeout=timeout) as client:
            status = await client.account_status()
        if heybox_id and status.get("logged_in") and str(status.get("user_id")) != heybox_id:
            return {"logged_in": False}
        return status

    async def close(self):
        async with self._lock:
            self._closed = True
            await self._stop()

    async def _stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            if self._login["state"] in ("requesting", "wait", "scanned", "saving"):
                self._login = {"state": "canceled"}

    async def cancel(self):
        if response := self._unauthorized():
            return response
        async with self._lock:
            await self._stop()
            self._login = {"state": "canceled"}
            return json_response(self._snapshot())

    async def check(self):
        if response := self._unauthorized():
            return response
        async with self._lock:
            cookie = self.plugin._cookie()
            heybox_id = str(self.plugin._c("heybox_id", "") or "").strip()
            try:
                if not cookie:
                    self._account = {"logged_in": False}
                else:
                    # 使用独立实例，防止检查旧账号的返回值覆盖正在切换的新账号。
                    status = await self._verify_account(cookie, heybox_id)
                    if cookie == self.plugin._cookie() and heybox_id == str(self.plugin._c("heybox_id", "") or "").strip():
                        self._account = {key: status[key] for key in ("logged_in", "nickname", "user_id") if key in status}
            except Exception:
                if cookie == self.plugin._cookie() and heybox_id == str(self.plugin._c("heybox_id", "") or "").strip():
                    self._account = {"logged_in": None, "check_unavailable": True}
            return json_response(self._snapshot())

    async def logout(self):
        if response := self._unauthorized():
            return response
        payload = await request.json(default={})
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            return error_response("清除本地登录态需要显式确认 confirm=true", status_code=400)
        async with self._lock:
            await self._stop()
            try:
                self.plugin._save_credentials(dict.fromkeys(("cookie", "user_pkey", "x_xhh_tokenid", "heybox_id"), ""), backup=False)
            except Exception:
                return error_response("清除配置失败，请检查插件配置文件权限", status_code=500)
            self._account = {"logged_in": False}
            self._login = {"state": "idle"}
            return json_response(self._snapshot())
