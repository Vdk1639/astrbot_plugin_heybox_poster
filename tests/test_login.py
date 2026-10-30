"""扫码协议回归，不访问真实账号或网络。"""

import base64
import hashlib
import pathlib
import sys
import unittest
import urllib.parse
from email.message import Message
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from astrbot_plugin_heybox_poster.core.client import NetworkError, XhhClient, XhhError
from astrbot_plugin_heybox_poster.core.login import QrLoginClient


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_ticket_preserves_both_supported_poll_query_shapes(self):
        for query in ("qr=test&app=heybox", "qr_ticket=test&device=web&session=opaque&empty="):
            with self.subTest(query=query):
                client = QrLoginClient()
                client.request = mock.AsyncMock(return_value={"status": "ok", "result": {
                    "qr_url": "https://api.xiaoheihe.cn/account/qr_login/?" + query,
                    "expire": 180,
                }})
                ticket = await client.ticket()
                self.assertEqual(ticket["expire"], 120)
                params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
                self.assertEqual(ticket["params"], params)
                client.request.return_value = {"status": "ok", "result": {"error": "scanned"}}
                self.assertEqual(await client.poll(params), "scanned")
                self.assertEqual(client.request.call_args.kwargs["params"], params)
                signed = urllib.parse.parse_qs(urllib.parse.urlsplit(client._build_url(
                    "/account/qr_state/", params, False
                )).query, keep_blank_values=True)
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

    async def test_poll_normalizes_states_and_keeps_unknown_pending(self):
        client = QrLoginClient()
        for raw, expected in (("wait", "wait"), ("ready", "scanned"), ("ok", "ok"), ("scanned", "scanned"),
                              ("timeout", "expired"), ("expired", "expired"), ("denied", "canceled"),
                              ("success", "ok"), ("CONFIRMED", "ok"), ("", "wait"), ("login", "wait"),
                              ("secret-value", "wait"), ("failed", "failed")):
            client.request = mock.AsyncMock(return_value={"status": "ok", "result": {"error": raw}})
            self.assertEqual(await client.poll({"qr": "x"}), expected)
        for raw, expected in ((0, "wait"), (1, "scanned"), (2, "ok"), (3, "expired"), (-1, "failed")):
            client.request.return_value = {"status": "ok", "result": {"qr_state": raw}}
            self.assertEqual(await client.poll({"qr": "x"}), expected)

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
            with self.assertRaises(NetworkError):
                await client.account_status()

    async def test_account_network_failure_is_not_reported_as_expired(self):
        client = XhhClient("user_pkey=x")
        for error in (NetworkError("secret-value"), XhhError("secret-value", status="failed")):
            client.request = mock.AsyncMock(side_effect=error)
            with self.assertRaises(NetworkError) as caught:
                await client.account_status()
            self.assertNotIn("secret-value", str(caught.exception))
        client.request = mock.AsyncMock(return_value={"status": "login"})
        self.assertEqual(await client.account_status(), {"logged_in": False})

    async def test_json_only_credentials_match_reference_and_keep_client_identity(self):
        client = QrLoginClient()
        client.request = mock.AsyncMock(return_value={"status": "ok", "result": {
            "error": "ok", "heyboxid": "10001", "nickname": "MockUser", "pkey": "fixture-pkey",
        }})
        self.assertEqual(await client.poll({"qr": "x"}), "ok")
        with mock.patch("astrbot_plugin_heybox_poster.core.login.time.time", return_value=123), \
             mock.patch("astrbot_plugin_heybox_poster.core.login.secrets.token_hex", side_effect=["a", "b", "c"]):
            cookie = client.login_cookie()
        from http.cookies import SimpleCookie
        parsed = SimpleCookie(cookie)
        expected = b"".join(hashlib.md5(x.encode(), usedforsecurity=False).digest() for x in ("123", "a", "b", "c")) + b"\x00"
        self.assertEqual(base64.b64decode(parsed["x_xhh_tokenid"].value), expected)
        self.assertEqual(parsed["user_pkey"].value, "fixture-pkey")
        self.assertEqual(client.login_heybox_id(), "10001")
        self.assertEqual(client.nickname, "MockUser")
        self.assertEqual(client.login_cookie(), cookie)
        restored = XhhClient(cookie)
        for current, path in ((client, "/account/get_qrcode_url/"), (client, "/account/qr_state/"),
                              (restored, "/account/restore_login")):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(current._build_url(path, {}, False)).query)
            self.assertEqual(query["device_id"], [client.device_id])

    async def test_nested_and_embedded_credentials_and_invalid_values(self):
        for key in ("user", "account", "profile", "account_detail"):
            client = QrLoginClient()
            client.request = mock.AsyncMock(return_value={"status": "ok", "result": {"state": 2, key: {
                "uid": "42", "username": "tester", "cookies": {"user_pkey": "p", "x_xhh_tokenid": "t"},
            }}})
            await client.poll({"qr": "x"})
            self.assertIn("user_pkey=p", client.login_cookie())
            self.assertEqual(client.login_heybox_id(), "42")
            self.assertEqual(client.nickname, "tester")
        for creds in ({"heyboxid": "42"}, {"heyboxid": "42", "pkey": "p\r\nInjected: x"},
                      {"heyboxid": "42", "pkey": {"bad": "shape"}}):
            client = QrLoginClient()
            client.request = mock.AsyncMock(return_value={"status": "ok", "result": {"error": "ok", **creds}})
            await client.poll({"qr": "x"})
            with self.assertRaises(XhhError):
                client.login_cookie()

    async def test_qr_identity_comes_from_new_response_or_cookie_aliases(self):
        for result, extra in (({"heyboxid": "42"}, {}), ({"heybox_id": 42}, {}),
                              ({}, {"heybox_id": "42"}), ({}, {"user_heybox_id": "42"})):
            with self.subTest(result=result, extra=extra):
                client = self._login_with_cookies(extra)
                client.request = mock.AsyncMock(return_value={"status": "ok", "result": {"error": "ok", **result}})
                self.assertEqual(await client.poll({"qr": "x"}), "ok")
                self.assertEqual(client.login_heybox_id(), "42")

    async def test_missing_invalid_or_conflicting_qr_identity_is_rejected(self):
        for result, extra in (({}, {}), ({"heyboxid": "secret-value"}, {}),
                              ({"heyboxid": True}, {}), ({"heyboxid": "４２"}, {}),
                              ({"heyboxid": "42"}, {"user_heybox_id": "99"})):
            client = self._login_with_cookies(extra)
            client.request = mock.AsyncMock(return_value={"status": "ok", "result": {"error": "ok", **result}})
            await client.poll({"qr": "x"})
            with self.assertRaises(XhhError) as caught:
                client.login_heybox_id()
            self.assertNotIn("secret-value", str(caught.exception))

    def _login_with_cookies(self, extra):
        import urllib.request

        client = QrLoginClient()
        headers = Message()
        for name, value in {"user_pkey": "new-p", "x_xhh_tokenid": "new-t", **extra}.items():
            headers.add_header("Set-Cookie", f"{name}={value}; Domain=.xiaoheihe.cn; Path=/; Secure")
        client._jar.extract_cookies(mock.Mock(info=lambda: headers), urllib.request.Request("https://api.xiaoheihe.cn/account/qr_state/"))
        return client
