"""最小扫码诊断不输出凭据、不保存账号、不访问网络。"""

import pathlib
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import debug_qr


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def test_reference_profile_keeps_qr_query_and_matches_reference_headers(self):
        from urllib.parse import parse_qs, urlsplit
        from urllib.request import Request
        client = debug_qr.DiagnosticClient(lambda *args, **kwargs: None, reference=True)
        url = client._build_url("/account/qr_state/", {"qr": "secret", "app": "custom", "extra": ""}, False)
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        self.assertEqual(query["app"], ["custom"])
        self.assertEqual(query["web_version"], ["2.5"])
        self.assertEqual(query["extra"], [""])
        self.assertNotIn("x_client_version", query)
        request = Request(url, headers={"Origin": "https://www.xiaoheihe.cn", "User-Agent": "original"})
        debug_qr.HttpObserver(lambda *args, **kwargs: None, True).https_request(request)
        self.assertIsNone(request.get_header("Origin"))
        self.assertEqual(request.get_header("User-agent"), "AstrBot-Xiaoheihe-Adapter/1.1.3")

    def test_summary_redacts_all_values_except_known_state_markers(self):
        payload = {"status": "ok", "result": {
            "error": "secret-pkey", "error_msg": "secret-message", "qr_url": "secret-qr-url",
            "profile": {"heyboxid": "secret-id", "pkey": "secret-pkey", "nickname": "secret-name"},
            "cookies": {"x_xhh_tokenid": "secret-token"},
        }}
        summary = debug_qr.summarize(payload, {"user_pkey": "secret-pkey", "secret-cookie-name": "secret-value"})
        self.assertNotIn("secret", str(summary))
        self.assertTrue(summary["has_pkey"])
        self.assertTrue(summary["has_id"])
        self.assertTrue(summary["has_server_token"])
        self.assertEqual(summary["other_cookie_count"], 1)
        self.assertEqual(summary["markers"], {"error": "<other>"})

    async def test_success_has_separate_confirmation_extraction_and_verification_events(self):
        directory = pathlib.Path(__file__).resolve().parents[1] / (".qr_test_" + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(directory.rmdir)
        self.addCleanup(lambda: [item.unlink() for item in directory.iterdir()])
        login = debug_qr.QrLoginClient()
        login.ticket = mock.AsyncMock(return_value={"url": "https://api.xiaoheihe.cn/account/qr_login/?qr=secret",
                                                    "params": {"qr": "secret"}, "expire": 120})
        login.request = mock.AsyncMock(side_effect=[
            {"status": "ok", "result": {"error": "ready"}},
            {"status": "ok", "result": {"error": "ok", "heyboxid": "42", "pkey": "secret-pkey"}},
        ])
        login.close = mock.AsyncMock()
        authenticated = mock.MagicMock()
        authenticated.__aenter__.return_value.request = mock.AsyncMock(return_value={"status": "login"})
        events = []
        with mock.patch.object(debug_qr, "DiagnosticClient", return_value=login), \
             mock.patch.object(debug_qr, "XhhClient", return_value=authenticated), \
             mock.patch.object(debug_qr.asyncio, "sleep", new=mock.AsyncMock()):
            code = await debug_qr.diagnose(directory, lambda event, **fields: events.append((event, fields)))
        self.assertEqual(code, 0)
        self.assertEqual([fields["state"] for event, fields in events if event == "poll"], ["scanned", "ok"])
        self.assertEqual(len([1 for event, _ in events if event == "verification"]), 2)
        self.assertEqual(events[-1][1]["outcome"], "server_confirmed_credentials_extracted")
        self.assertFalse(events[-1][1]["credentials_saved"])
        self.assertNotIn("secret", str(events))
        self.assertEqual([item.name for item in directory.iterdir()], ["qr.png"])
        login.close.assert_awaited_once()
