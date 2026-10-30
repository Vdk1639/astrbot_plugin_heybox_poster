"""扫码协议回归，不访问真实账号或网络。"""

import pathlib
import sys
import unittest
import urllib.parse
from email.message import Message
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from astrbot_plugin_heybox_poster.core.client import XhhClient, XhhError
from astrbot_plugin_heybox_poster.core.login import QrLoginClient


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_preserves_both_supported_poll_query_shapes(self):
        for query in ("qr=test&app=heybox", "qr_ticket=test&device=web"):
            with self.subTest(query=query):
                client = QrLoginClient()
                client.request = mock.AsyncMock(return_value={"status": "ok", "result": {
                    "qr_url": "https://api.xiaoheihe.cn/account/qr_login/?" + query,
                    "expire": 180,
                }})
                ticket = await client.ticket()
                self.assertEqual(ticket["expire"], 120)
                params = dict(urllib.parse.parse_qsl(query))
                self.assertEqual(ticket["params"], params)
                client.request.return_value = {"status": "ok", "result": {"error": "scanned"}}
                self.assertEqual(await client.poll(params), "scanned")
                self.assertEqual(client.request.call_args.kwargs["params"], params)
                signed = urllib.parse.parse_qs(urllib.parse.urlsplit(client._build_url(
                    "/account/qr_state/", params, False
                )).query)
                self.assertTrue(signed["hkey"])
                for key, value in params.items():
                    self.assertEqual(signed[key], [value])

    async def test_ticket_rejects_untrusted_urls_and_invalid_expiry(self):
        for url, expire in (
            ("https://evil.example/account/qr_login/?qr=x", 120),
            ("http://api.xiaoheihe.cn/account/qr_login/?qr=x", 120),
            ("https://api.xiaoheihe.cn/account/qr_login/?qr=", 120),
            ("https://api.xiaoheihe.cn/other/?qr=x", 120),
            ("https://api.xiaoheihe.cn/account/qr_login/?qr=x", 0),
            ("https://api.xiaoheihe.cn/account/qr_login/?qr=x", "bad"),
        ):
            with self.subTest(url=url, expire=expire):
                client = QrLoginClient()
                client.request = mock.AsyncMock(return_value={"status": "ok", "result": {"qr_url": url, "expire": expire}})
                with self.assertRaises(XhhError):
                    await client.ticket()

    async def test_poll_normalizes_states_and_rejects_unknown(self):
        client = QrLoginClient()
        for raw, expected in (("wait", "wait"), ("ready", "wait"), ("ok", "ok"), ("scanned", "scanned"),
                              ("timeout", "expired"), ("expired", "expired"), ("denied", "canceled")):
            client.request = mock.AsyncMock(return_value={"status": "ok", "result": {"error": raw}})
            self.assertEqual(await client.poll({"qr": "x"}), expected)
        client.request.return_value = {"status": "ok", "result": {"error": "secret-value"}}
        with self.assertRaises(XhhError) as caught:
            await client.poll({"qr": "x"})
        self.assertNotIn("secret-value", str(caught.exception))

    async def test_cookie_jar_collects_set_cookie_and_sessions_are_isolated(self):
        client = QrLoginClient()
        other = QrLoginClient()
        headers = Message()
        headers.add_header("Set-Cookie", "user_pkey=secret-p; Domain=.xiaoheihe.cn; Path=/; Secure; HttpOnly")
        headers.add_header("Set-Cookie", "x_xhh_tokenid=secret-t; Domain=.xiaoheihe.cn; Path=/; Secure")
        response = mock.Mock(info=lambda: headers)
        import urllib.request

        client._jar.extract_cookies(response, urllib.request.Request("https://api.xiaoheihe.cn/account/qr_state/"))
        self.assertIn("user_pkey=secret-p", client.login_cookie())
        self.assertIn("x_xhh_tokenid=secret-t", client.login_cookie())
        with self.assertRaises(XhhError):
            other.login_cookie()
        # 只有 tokenid 不能替换当前账号。
        client._jar.clear(".xiaoheihe.cn", "/", "user_pkey")
        with self.assertRaises(XhhError):
            client.login_cookie()

    async def test_restore_login_requires_identity_and_accepts_account_detail(self):
        client = XhhClient("user_pkey=x")
        for result in ({"profile": {"heybox_id": "42", "nickname": "tester"}},
                       {"account_detail": {"userid": "42", "nickname": "tester"}},
                       {"heyboxid": "42", "nickname": "tester"}):
            client.request = mock.AsyncMock(return_value={"status": "ok", "result": result})
            self.assertEqual(await client.account_status(), {"logged_in": True, "nickname": "tester", "user_id": "42"})
        for result in ({}, {"profile": []}, [], "invalid"):
            client.request.return_value = {"status": "ok", "result": result}
            self.assertFalse((await client.account_status())["logged_in"])
