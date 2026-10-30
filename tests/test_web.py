"""用 AstrBot API 桩检查鉴权、扫码任务、凭据保存和失败回滚。"""

import asyncio
import json
import pathlib
import sys
import types
import unittest
import uuid
from unittest import mock

from test_plugin_smoke import XhhPosterPlugin
from astrbot_plugin_heybox_poster.core.client import XhhError
from astrbot_plugin_heybox_poster.core.client import XhhClient
from astrbot_plugin_heybox_poster.core.outbox import Outbox

web_stub = types.ModuleType("astrbot.api.web")
web_stub.request = types.SimpleNamespace(username="admin")
web_stub.json_response = lambda data: data
web_stub.error_response = lambda message, status_code=400: {"error": message, "code": status_code}
sys.modules["astrbot.api.web"] = web_stub
from astrbot_plugin_heybox_poster.core.web import AccountWebUI


class Config(dict):
    def save_config(self):
        self.saved = dict(self)


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = pathlib.Path(__file__).resolve().parents[1] / f".login_test_{uuid.uuid4().hex}"
        self.directory.mkdir()
        self.addCleanup(self._cleanup_directory)
        self.box = Outbox(":memory:")
        self.config = Config(cookie="user_pkey=old-p; x_xhh_tokenid=old-t", user_pkey="old-p", x_xhh_tokenid="old-t", heybox_id="1", dry_run=False)
        self.plugin = XhhPosterPlugin(None, self.config)
        self.plugin._get_outbox = lambda: self.box
        self.plugin._default_outbox_path = lambda: self.directory / "outbox.sqlite3"
        self.web = AccountWebUI(self.plugin)
        self.plugin.web = self.web
        web_stub.request.username = "admin"
        web_stub.request.json = mock.AsyncMock(return_value={"confirm": True})

    def _cleanup_directory(self):
        for file in self.directory.iterdir():
            file.unlink()
        self.directory.rmdir()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.box.close()

    async def test_all_routes_deny_unauthenticated_requests_before_side_effects(self):
        web_stub.request.username = None
        for method in (self.web.status, self.web.qr, self.web.cancel, self.web.check, self.web.logout):
            response = await method()
            self.assertEqual(response["code"], 401)
        self.assertIsNone(self.web._task)
        self.assertIn("old-p", self.plugin._cookie())

    async def test_routes_register_with_expected_http_methods(self):
        context = mock.Mock()
        self.web.register(context)
        routes = {call.args[0]: call.args[2] for call in context.register_web_api.call_args_list}
        self.assertEqual(len(routes), 5)
        self.assertEqual(routes["/astrbot_plugin_heybox_poster/auth/status"], ["GET"])
        for route, methods in routes.items():
            if not route.endswith("status"):
                self.assertEqual(methods, ["POST"])

    async def test_confirmed_credentials_save_without_second_network_gate(self):
        old_client = self.plugin._get_client()
        login = self._login_client(["wait", "scanned", "ok"])
        with mock.patch("astrbot_plugin_heybox_poster.core.web.XhhClient") as factory, \
             mock.patch("astrbot_plugin_heybox_poster.core.web.asyncio.sleep", new=mock.AsyncMock()):
            await self.web._poll(login, {"params": {"qr": "x"}, "expire": 120})
        factory.assert_not_called()
        self.assertEqual(self.config["heybox_id"], "42")
        self.assertEqual(self.config.saved["user_pkey"], "new-p")
        self.assertIsNot(self.plugin._get_client(), old_client)
        self.assertEqual(json.loads((self.directory / "login_backup.json").read_text())["heybox_id"], "1")
        response = await self.web.status()
        self.assertEqual(response["login"]["state"], "success")
        self.assertEqual(response["account"]["nickname"], "tester")
        for value in ("new-p", "new-t", "old-p", "cookie", "user_pkey"):
            self.assertNotIn(value, json.dumps(response))
        login.close.assert_awaited_once()

    async def test_save_failure_restores_old_credentials_and_client(self):
        original = dict(self.config)
        old_client = self.plugin._get_client()
        with mock.patch.object(self.config, "save_config", side_effect=OSError("sensitive-value")):
            with self.assertRaises(XhhError) as caught:
                self.plugin._save_login("user_pkey=new-p; x_xhh_tokenid=new-t", {"user_id": "42"})
        self.assertNotIn("sensitive-value", str(caught.exception))
        self.assertEqual(self.config, original)
        self.assertIs(self.plugin._get_client(), old_client)

    async def test_invalid_credentials_never_overwrite(self):
        original = dict(self.config)
        for cookie, uid in (("user_pkey=only", "42"), ("user_pkey=p; x_xhh_tokenid=t", "not-an-id")):
            with self.assertRaises(XhhError):
                self.plugin._save_login(cookie, {"user_id": uid})
        self.assertEqual(self.config, original)

    async def test_cancel_logout_and_terminate_stop_login_tasks(self):
        started = asyncio.Event()
        canceled = asyncio.Event()

        async def pending():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        self.web._task = asyncio.create_task(pending())
        await started.wait()
        response = await self.web.cancel()
        self.assertTrue(canceled.is_set())
        self.assertEqual(response["login"]["state"], "canceled")
        self.assertIn("old-p", self.plugin._cookie())
        self.plugin._save_login("user_pkey=new-p; x_xhh_tokenid=new-t", {"user_id": "42"})
        self.assertTrue((self.directory / "login_backup.json").exists())
        response = await self.web.logout()
        self.assertFalse(response["account"]["has_credentials"])
        self.assertEqual(self.config["heybox_id"], "")
        self.assertFalse((self.directory / "login_backup.json").exists())
        await self.web.close()
        self.assertEqual((await self.web.qr())["code"], 503)

    async def test_expiry_cancellation_and_explicit_failure_preserve_original(self):
        original = dict(self.config)
        for states, expected in ((["failed"], "failed"), (["expired"], "expired"), (["canceled"], "canceled")):
            with mock.patch("astrbot_plugin_heybox_poster.core.web.asyncio.sleep", new=mock.AsyncMock()):
                await self.web._poll(self._login_client(states), {"params": {}, "expire": 120})
            self.assertEqual(self.web._login["state"], expected)
            self.assertEqual(self.config, original)
            self.assertNotIn("secret-cookie", json.dumps(await self.web.status()))
        await self.web._poll(self._login_client([]), {"params": {}, "expire": .001})
        self.assertEqual(self.web._login["state"], "expired")

    async def test_qr_image_and_unload_cancellation(self):
        login = self._login_client(["wait"])
        login.ticket = mock.AsyncMock(return_value={"url": "https://api.xiaoheihe.cn/account/qr_login/?qr=x", "params": {"qr": "x"}, "expire": 120})
        with mock.patch("astrbot_plugin_heybox_poster.core.web.QrLoginClient", return_value=login):
            response = await self.web.qr()
        self.assertTrue(response["login"]["qr_image"].startswith("data:image/png;base64,iVBOR"))
        self.assertEqual(response["login"]["state"], "wait")
        task = self.web._task
        # 让任务进入 finally，验证卸载释放其独立会话。
        await asyncio.sleep(0)
        await self.plugin.terminate()
        self.assertTrue(task.done())
        self.assertEqual(self.web._login["state"], "canceled")
        login.close.assert_awaited_once()

    async def test_qr_failure_does_not_leak_server_payload(self):
        login = self._login_client([])
        login.ticket = mock.AsyncMock(side_effect=XhhError("secret-value"))
        with mock.patch("astrbot_plugin_heybox_poster.core.web.QrLoginClient", return_value=login):
            response = await self.web.qr()
        self.assertEqual(response["code"], 502)
        self.assertNotIn("secret-value", json.dumps(response))
        login.close.assert_awaited_once()

    async def test_check_returns_only_public_account_fields(self):
        authenticated = mock.MagicMock()
        authenticated.__aenter__.return_value.account_status = mock.AsyncMock(return_value={
            "logged_in": True, "user_id": "1", "nickname": "tester", "cookie": "secret-value",
        })
        with mock.patch("astrbot_plugin_heybox_poster.core.web.XhhClient", return_value=authenticated) as factory:
            response = await self.web.check()
        self.assertEqual(factory.call_args.kwargs["heybox_id"], "1")
        self.assertTrue(response["account"]["logged_in"])
        self.assertNotIn("secret-value", json.dumps(response))
        await self.web.logout()
        self.assertFalse((await self.web.check())["account"]["logged_in"])

    async def test_rate_limit_after_account_switch_is_recorded_for_original_account(self):
        from astrbot.api.event import AstrMessageEvent
        from astrbot_plugin_heybox_poster.core.client import RateLimited

        class OldClient:
            async def publish(inner, *args, **kwargs):
                self.config["heybox_id"] = "2"
                raise RateLimited("too fast")

        self.plugin._get_client = lambda: OldClient()
        event = AstrMessageEvent("123", role="admin", platform="aiocqhttp")
        result = await self.plugin.tool_publish(event, "test", "test", topic_ids="43")
        self.assertEqual(result, "too fast")
        self.assertEqual(self.plugin._rate_limit_block("qq:123"), "")
        self.assertGreater(self.box.cooldown_until("id:1:qq:123"), 0)

    async def test_dry_run_with_explicit_topics_does_not_require_login(self):
        from astrbot.api.event import AstrMessageEvent

        self.config.update(cookie="", user_pkey="", x_xhh_tokenid="", dry_run=True)
        result = await self.plugin.tool_publish(AstrMessageEvent(role="admin"), "test", "test", topic_ids="43")
        self.assertIn("dry-run", result)

    async def test_logout_requires_literal_confirmation_before_any_side_effects(self):
        self.plugin._save_login("user_pkey=new-p; x_xhh_tokenid=new-t", {"user_id": "42"})
        original = dict(self.config)
        self.web._login = {"state": "wait"}
        with mock.patch.object(self.web, "_stop", new=mock.AsyncMock()) as stop:
            for payload in ({}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1}, [], None):
                web_stub.request.json.return_value = payload
                response = await self.web.logout()
                self.assertEqual(response["code"], 400)
                self.assertEqual(self.config, original)
                self.assertTrue((self.directory / "login_backup.json").exists())
                self.assertEqual(self.web._login["state"], "wait")
            stop.assert_not_awaited()

    async def test_invalid_qr_identity_never_replaces_existing_credentials(self):
        original = dict(self.config)
        login = self._login_client(["ok"])
        login.login_heybox_id = mock.Mock(side_effect=XhhError("conflicting identities"))
        with mock.patch("astrbot_plugin_heybox_poster.core.web.asyncio.sleep", new=mock.AsyncMock()):
            await self.web._poll(login, {"params": {}, "expire": 120})
        self.assertEqual(self.web._login["state"], "failed")
        self.assertEqual(self.config, original)

    async def test_failure_reports_stage_without_leaking_error_details(self):
        login = self._login_client(["ok"])
        login.login_heybox_id = mock.Mock(side_effect=XhhError("secret-value"))
        with mock.patch("astrbot_plugin_heybox_poster.core.web.asyncio.sleep", new=mock.AsyncMock()), \
             mock.patch("astrbot_plugin_heybox_poster.core.web.logger.warning") as warning:
            await self.web._poll(login, {"params": {}, "expire": 120})
        self.assertEqual(self.web._login["state"], "failed")
        self.assertIn("账号 ID", self.web._login["message"])
        self.assertIn("账号 ID", warning.call_args.args[0])
        self.assertNotIn("secret-value", warning.call_args.args[0])
        self.assertNotIn("secret-value", json.dumps(await self.web.status()))

    async def test_first_login_then_check_sends_new_id_in_signed_requests(self):
        from urllib.parse import parse_qs, urlsplit

        self.config.update(cookie="", user_pkey="", x_xhh_tokenid="", heybox_id="")
        requests = []

        def server(client, method, url, form):
            requests.append(url)
            if parse_qs(urlsplit(url).query).get("heybox_id") != ["42"]:
                return {"status": "login"}
            return {"status": "ok", "result": {"profile": {"heybox_id": "42", "nickname": "tester"}}}

        with mock.patch.object(XhhClient, "_request_sync", autospec=True, side_effect=server), \
             mock.patch("astrbot_plugin_heybox_poster.core.web.asyncio.sleep", new=mock.AsyncMock()):
            await self.web._poll(self._login_client(["ok"]), {"params": {}, "expire": 120})
            self.assertEqual(self.web._login["state"], "success")
            self.assertTrue((await self.web.check())["account"]["logged_in"])
        self.assertEqual(self.config["heybox_id"], "42")
        self.assertEqual(len(requests), 1)
        self.assertTrue(all("hkey=" in url for url in requests))

    def _login_client(self, states):
        return types.SimpleNamespace(
            poll=mock.AsyncMock(side_effect=states), timeout=20,
            login_cookie=lambda: "user_pkey=new-p; x_xhh_tokenid=new-t",
            login_heybox_id=lambda: "42",
            nickname="tester",
            close=mock.AsyncMock(),
        )

    async def test_transient_poll_failures_retry_and_preserve_scanned_progress(self):
        login = self._login_client(["scanned", XhhError("secret-value"), XhhError("secret-value"),
                                    XhhError("secret-value"), "wait", "ok"])
        snapshots = []

        async def delay(seconds):
            snapshots.append((seconds, dict(self.web._login)))

        with mock.patch("astrbot_plugin_heybox_poster.core.web.asyncio.sleep", side_effect=delay), \
             mock.patch("astrbot_plugin_heybox_poster.core.web.logger.warning") as warning:
            await self.web._poll(login, {"params": {}, "expire": 120})
        self.assertEqual([s for s, _ in snapshots], [3, 3, 2, 4, 8, 3])
        self.assertEqual(snapshots[4][1]["state"], "scanned")
        self.assertEqual(snapshots[4][1]["retries"], 3)
        self.assertFalse(snapshots[5][1]["retrying"])
        self.assertEqual(snapshots[5][1]["state"], "scanned")
        self.assertEqual(self.web._login["state"], "success")
        self.assertNotIn("secret-value", str(warning.call_args_list))

    async def test_ready_and_endless_network_failures_expire_without_saving(self):
        original = dict(self.config)
        real_sleep = asyncio.sleep
        for value in ("scanned", XhhError("secret-value")):
            login = self._login_client([])
            if isinstance(value, Exception):
                login.poll.side_effect = value
            else:
                login.poll.side_effect = None
                login.poll.return_value = value
            async def delay(seconds):
                await real_sleep(.001)
            with mock.patch("astrbot_plugin_heybox_poster.core.web.asyncio.sleep", side_effect=delay):
                await self.web._poll(login, {"params": {}, "expire": .02})
            self.assertEqual(self.web._login["state"], "expired")
            self.assertEqual(self.config, original)
            login.close.assert_awaited_once()

    async def test_check_failure_is_visible_without_marking_credentials_invalid(self):
        with mock.patch.object(self.web, "_verify_account", side_effect=XhhError("secret-value")):
            response = await self.web.check()
        self.assertTrue(response["account"]["has_credentials"])
        self.assertIsNone(response["account"]["logged_in"])
        self.assertTrue(response["account"]["check_unavailable"])
        self.assertNotIn("secret-value", json.dumps(response))

    async def test_old_account_check_cannot_overwrite_switched_account(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.config.update(cookie="user_pkey=old-p; x_xhh_tokenid=old-t", heybox_id="1")
                started = asyncio.Event()
                finish = asyncio.Event()

                async def verify(*args):
                    started.set()
                    await finish.wait()
                    if fail:
                        raise XhhError("old-account-error")
                    return {"logged_in": True, "nickname": "old", "user_id": "1"}

                with mock.patch.object(self.web, "_verify_account", side_effect=verify):
                    task = asyncio.create_task(self.web.check())
                    await asyncio.wait_for(started.wait(), timeout=1)
                    self.plugin._save_login("user_pkey=new-p; x_xhh_tokenid=new-t", {"user_id": "42"})
                    self.web._account = {"logged_in": True, "nickname": "new"}
                    finish.set()
                    response = await asyncio.wait_for(task, timeout=1)
                self.assertEqual(response["account"]["user_id"], "42")
                self.assertEqual(response["account"]["nickname"], "new")
                self.assertTrue(response["account"]["logged_in"])
                self.assertNotIn("check_unavailable", response["account"])

    async def test_json_only_qr_confirmation_saves_real_parser_credentials(self):
        from astrbot_plugin_heybox_poster.core.login import QrLoginClient
        login = QrLoginClient()
        login.request = mock.AsyncMock(side_effect=[
            {"status": "ok", "result": {"error": "ready", "error_msg": "等待点击登录"}},
            {"status": "ok", "result": {"error": "ok", "profile": {
                "heyboxid": "42", "pkey": "json-p", "nickname": "tester",
            }}},
        ])
        with mock.patch("astrbot_plugin_heybox_poster.core.web.asyncio.sleep", new=mock.AsyncMock()), \
             mock.patch.object(self.web, "_verify_account", side_effect=AssertionError("no extra gate")):
            await self.web._poll(login, {"params": {"qr": "test-ticket"}, "expire": 120})
        self.assertEqual(self.web._login["state"], "success")
        self.assertEqual(self.config.saved["user_pkey"], "json-p")
        self.assertEqual(self.config.saved["heybox_id"], "42")
        self.assertTrue(self.config.saved["x_xhh_tokenid"])
        self.assertEqual(self.plugin._get_client().device_id, login.device_id)
        self.assertNotIn("json-p", json.dumps(await self.web.status()))
