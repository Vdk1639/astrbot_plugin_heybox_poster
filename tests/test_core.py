"""签名与发帖表单的离线测试（不联网、不需要 AstrBot）。

运行：``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import pathlib
import sys
import time
import unittest
import urllib.error
import urllib.parse
import uuid
from unittest import mock
from http.client import IncompleteRead

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_heybox_poster.core.client import (  # noqa: E402
    CaptchaRequired,
    NetworkError,
    RateLimited,
    XhhClient,
    XhhError,
    build_post_form,
)
from astrbot_plugin_heybox_poster.core.outbox import Outbox  # noqa: E402
from astrbot_plugin_heybox_poster.core.sign import (  # noqa: E402
    hkey,
    normalize_path,
    signed_params,
    workshop_rnd,
)

#: 真实抓包样本（56dz/xiaoheihe-api 自检数据）
SAMPLES = [
    ("/bbs/app/feeds", 1793159576, "E228D657D7AE3182EFDA4283E2C32F06", "VS2S006"),
    ("/bbs/app/feeds/banner", 1793159576, "B358F4066EC902AACCE68BBCA66EDDE2", "D7PV763"),
    ("/account/restore_login", 1793159576, "22BF3B0F660816C5929439F2E9D80299", "YT0TV70"),
    ("/bbs/app/topic/categories", 1793159576, "D78865C774303864E97ECB6119BC30C4", "VUP1022"),
    ("/bbs/app/api/search/found", 1793159576, "4399A965B2891DC60660993ADF5F3C44", "1371749"),
    (
        "/bbs/app/api/search/welcome_page/v2",
        1793159576,
        "DAFC50032B4C5B13E91EB517AE9EE717",
        "U00UU96",
    ),
]


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.status = status
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class SignTests(unittest.TestCase):
    def test_hkey_matches_captured_samples(self):
        for path, ts, nonce, expected in SAMPLES:
            with self.subTest(path=path):
                self.assertEqual(hkey(path, ts, nonce), expected)

    def test_normalize_path(self):
        self.assertEqual(normalize_path("/bbs/app/feeds?x=1"), "/bbs/app/feeds/")
        self.assertEqual(normalize_path("bbs/app/feeds/"), "/bbs/app/feeds/")
        self.assertEqual(normalize_path("//bbs//app//feeds"), "/bbs/app/feeds/")

    def test_signed_params_shape(self):
        params = signed_params("/bbs/app/api/link/post", 1793159576, "A" * 32)
        self.assertEqual(params["_time"], 1793159576)
        self.assertEqual(params["nonce"], "A" * 32)
        self.assertRegex(params["hkey"], r"^[A-Z0-9]{5}\d{2}$")

    def test_workshop_rnd_is_hmac_v15(self):
        params = signed_params("/bbs/app/api/link/post", 1793159576, "B" * 32)
        rnd = workshop_rnd(params)
        self.assertTrue(rnd.startswith("15:"))
        self.assertEqual(len(rnd), 3 + 64)
        # 相同 nonce/_time 必然得到相同结果，便于回归
        self.assertEqual(rnd, workshop_rnd(dict(params)))


class PostFormTests(unittest.TestCase):
    def test_dynamic_form(self):
        form = build_post_form("标题", "正文", topic_ids=["43"])
        self.assertEqual(form["link_tag"], "27")
        self.assertEqual(form["topic_ids"], "43")
        self.assertEqual(form["post_type"], "1")
        self.assertEqual(json.loads(form["text"]), [{"type": "text", "text": "正文"}])
        self.assertNotIn("words_count", form)
        self.assertNotIn("draft", form)

    def test_article_form_has_required_fields(self):
        form = build_post_form("标题", "<p>正文</p>", mode="article", topic_ids=["11"], draft=True)
        self.assertEqual(form["link_tag"], "11")
        self.assertEqual(form["draft"], "1")
        self.assertEqual(form["declaration"], "1")
        self.assertEqual(form["extra_declaration"], "-1")
        self.assertEqual(form["words_count"], str(len("<p>正文</p>")))
        self.assertEqual(json.loads(form["text"]), [{"type": "html", "text": "<p>正文</p>"}])

    def test_topic_is_required(self):
        with self.assertRaises(ValueError):
            build_post_form("标题", "正文", topic_ids=[])

    def test_empty_title_or_content(self):
        with self.assertRaises(ValueError):
            build_post_form("", "正文", topic_ids=["1"])
        with self.assertRaises(ValueError):
            build_post_form("标题", "  ", topic_ids=["1"])


class ClientTests(unittest.TestCase):
    def _outbox(self) -> Outbox:
        path = ROOT / f".outbox_test_{uuid.uuid4().hex}.sqlite3"
        outbox = Outbox(str(path))

        def cleanup() -> None:
            outbox.close()
            for suffix in ("", "-wal", "-shm"):
                candidate = pathlib.Path(str(path) + suffix)
                if candidate.exists():
                    candidate.unlink()

        self.addCleanup(cleanup)
        return outbox

    def _client(self, payload: dict, *, list_payload: dict | None = None) -> tuple[XhhClient, list]:
        client = XhhClient("user_pkey=abc; x_xhh_tokenid=def", heybox_id="123")
        captured: list = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            if list_payload is not None and "/bbs/app/profile/user/link/list_v2" in req.full_url:
                return FakeResponse(list_payload)
            return FakeResponse(payload)

        patcher = mock.patch("urllib.request.urlopen", side_effect=fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client, captured

    def test_publish_sends_signed_form(self):
        client, captured = self._client(
            {"status": "ok", "result": {"link_id": "999"}},
            list_payload={"status": "ok", "result": {"post_links": []}},
        )
        result = asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
        self.assertEqual(result["link_id"], "999")

        req = [r for r in captured if r.get_method() == "POST"][0]
        self.assertEqual(req.get_method(), "POST")
        query = _query(req.full_url)
        self.assertIn("hkey", query)
        self.assertIn("nonce", query)
        self.assertTrue(query["_rnd"].startswith("15:"))
        self.assertEqual(query["heybox_id"], "123")
        self.assertEqual(query["app"], "heybox")

        body = dict(urllib.parse.parse_qsl(req.data.decode("utf-8")))
        self.assertEqual(body["link_tag"], "27")
        self.assertEqual(body["topic_ids"], "43")
        self.assertEqual(body["title"], "标题")
        self.assertIn("x-www-form-urlencoded", req.get_header("Content-type"))

    def test_comment_uses_write_signature(self):
        client, captured = self._client({"status": "ok", "result": {}})
        asyncio.run(client.comment(195173343, "测试评论"))
        query = _query(captured[0].full_url)
        self.assertTrue(query["_rnd"].startswith("15:"))
        body = dict(urllib.parse.parse_qsl(captured[0].data.decode("utf-8")))
        self.assertEqual(body["link_id"], "195173343")
        self.assertEqual(body["reply_id"], "-1")

    def test_delete_link_uses_write_signature(self):
        client, captured = self._client({"status": "ok", "result": {}})
        asyncio.run(client.delete_link("195381907"))
        query = _query(captured[0].full_url)
        self.assertTrue(query["_rnd"].startswith("15:"))
        body = dict(urllib.parse.parse_qsl(captured[0].data.decode("utf-8")))
        self.assertEqual(body["link_id"], "195381907")

    def test_delete_link_cleans_outbox(self):
        outbox = self._outbox()
        outbox.mark_pending("fp", {"title": "a"})
        outbox.mark_sent("fp", "195381907")
        client, _ = self._client({"status": "ok", "result": {}})
        client.outbox = outbox
        asyncio.run(client.delete_link("195381907"))
        self.assertEqual(outbox.remove_by_link_id("195381907"), 0)

    def test_show_captcha_raises(self):
        client, _ = self._client(
            {"status": "show_captcha", "result": {}, "msg": ""},
            list_payload={"status": "ok", "result": {"post_links": []}},
        )
        with self.assertRaises(CaptchaRequired):
            asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))

    def test_business_error_raises(self):
        client, _ = self._client(
            {"status": "failed", "msg": "请添加分区"},
            list_payload={"status": "ok", "result": {"post_links": []}},
        )
        with self.assertRaises(XhhError) as ctx:
            asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
        self.assertIn("请添加分区", str(ctx.exception))

    def test_rate_limited_raises(self):
        client, _ = self._client(
            {"status": "failed", "msg": "发帖频率过快，请稍后再试:10006#02"},
            list_payload={"status": "ok", "result": {"post_links": []}},
        )
        with self.assertRaises(RateLimited):
            asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))

    def test_rate_limited_clears_pending(self):
        outbox = self._outbox()
        client, _ = self._client(
            {"status": "failed", "msg": "发帖频率过快，请稍后再试:10006#02"},
            list_payload={"status": "ok", "result": {"post_links": []}},
        )
        client.outbox = outbox
        with self.assertRaises(RateLimited):
            asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
        self.assertEqual(outbox.pending_rows(), [])

    def test_explicit_business_rejection_clears_pending(self):
        outbox = self._outbox()
        client = XhhClient("user_pkey=abc; x_xhh_tokenid=def", heybox_id="123")
        client.outbox = outbox

        async def fail_request(method, path, *, params=None, form=None, write=False):
            raise XhhError("服务端拒绝请求", status="failed")

        client.request = fail_request
        with self.assertRaises(XhhError):
            asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
        self.assertEqual(outbox.pending_rows(), [])

    def test_network_error_wrapped(self):
        client, _ = self._client(
            {},
            list_payload={"status": "ok", "result": {"post_links": []}},
        )
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            with self.assertRaises(XhhError):
                asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))

    def test_search_topics_slices_locally(self):
        client = XhhClient("user_pkey=abc; x_xhh_tokenid=def")

        async def fake_request(method, path, *, params=None, form=None, write=False):
            return {
                "status": "ok",
                "result": {
                    "search_result": [
                        {"topic_id": str(i), "name": f"话题{i}"} for i in range(5)
                    ]
                },
            }

        client.request = fake_request
        topics = asyncio.run(client.search_topics("小黑盒", limit=3))
        self.assertEqual(len(topics), 3)

    def test_list_my_posts_normalizes(self):
        client = XhhClient("user_pkey=abc; x_xhh_tokenid=def", heybox_id="123")

        async def recent(limit=20):
            return [
                {"linkid": 10, "title": "甲"},
                {"link_id": "20", "title": "乙"},
                {"title": "无 id"},
            ]

        client._my_recent_links = recent
        posts = asyncio.run(client.list_my_posts())
        self.assertEqual(
            posts,
            [{"link_id": "10", "title": "甲"}, {"link_id": "20", "title": "乙"}],
        )

    def test_reconcile_claims_only_new_items(self):
        client = XhhClient("user_pkey=abc; x_xhh_tokenid=def", heybox_id="123")
        now = time.time()

        async def recent(limit=20):
            return [
                {"linkid": "1", "title": "标题", "create_time": int(now)},
                {"linkid": "2", "title": "标题", "create_time": int(now)},
            ]

        client._my_recent_links = recent
        self.assertEqual(
            asyncio.run(client._reconcile_link_id("标题", {"1"}, now - 5)),
            "2",
        )

    def test_reconcile_skips_old_timestamp(self):
        client = XhhClient("user_pkey=abc; x_xhh_tokenid=def", heybox_id="123")
        now = time.time()

        async def recent(limit=20):
            return [{"linkid": "3", "title": "标题", "create_time": int(now - 600)}]

        client._my_recent_links = recent
        self.assertEqual(
            asyncio.run(client._reconcile_link_id("标题", set(), now)),
            "",
        )

    def test_publish_outbox_dedup_skips_second_post(self):
        outbox = self._outbox()
        client, captured = self._client(
            {"status": "ok", "result": {"link_id": "999"}},
            list_payload={"status": "ok", "result": {"post_links": []}},
        )
        client.outbox = outbox
        first = asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
        self.assertEqual(first["link_id"], "999")

        posts_before = len([r for r in captured if r.get_method() == "POST"])
        second = asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
        posts_after = len([r for r in captured if r.get_method() == "POST"])

        self.assertTrue(second["duplicate"])
        self.assertEqual(second["link_id"], "999")
        self.assertEqual(posts_after, posts_before)

    def test_publish_network_error_marks_unknown(self):
        outbox = self._outbox()
        client, _ = self._client(
            {},
            list_payload={"status": "ok", "result": {"post_links": []}},
        )
        client.outbox = outbox
        with mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("boom")
        ):
            with self.assertRaises(NetworkError):
                asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
        self.assertEqual(len(outbox.unknown_rows()), 1)

    def test_different_publish_intents_are_not_duplicates(self):
        variants = [
            {"draft": True},
            {"mode": "article"},
            {"topic_ids": ["44"]},
            {"view_limit": 3},
            {"desc": "另一个摘要"},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                outbox = self._outbox()
                client = XhhClient("user_pkey=abc", outbox=outbox)
                client.request = mock.AsyncMock(
                    return_value={"status": "ok", "result": {"link_id": "1"}}
                )
                first = asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
                options = {"topic_ids": ["43"], **variant}
                second = asyncio.run(client.publish("标题", "正文", **options))
                self.assertFalse(first["duplicate"])
                self.assertFalse(second["duplicate"])
                self.assertEqual(client.request.await_count, 2)

    def test_accounts_have_separate_dedup_records(self):
        outbox = self._outbox()
        for account in ("first", "second"):
            client = XhhClient(f"user_pkey={account}; x_xhh_tokenid=token", outbox=outbox)
            client.request = mock.AsyncMock(
                return_value={"status": "ok", "result": {"link_id": account}}
            )
            result = asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
            self.assertFalse(result["duplicate"])
            client.request.assert_awaited_once()

    def test_token_refresh_preserves_account_dedup(self):
        outbox = self._outbox()
        for token in ("old", "new"):
            client = XhhClient(f"user_pkey=same; x_xhh_tokenid={token}", outbox=outbox)
            client.request = mock.AsyncMock(
                return_value={"status": "ok", "result": {"link_id": "1"}}
            )
            result = asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
            self.assertEqual(result["duplicate"], token == "new")

    def test_legacy_records_preserve_dedup_and_draft_distinction(self):
        for draft, status in ((False, "sent"), (True, "sent"), (False, "send_unknown")):
            with self.subTest(draft=draft, status=status):
                outbox = self._outbox()
                legacy = hashlib.sha1("标题\n正文".encode("utf-8")).hexdigest()
                form = build_post_form("标题", "正文", topic_ids=["43"], draft=draft)
                outbox.mark_pending(legacy, {"title": "标题", "form": form})
                if status == "sent":
                    outbox.mark_sent(legacy, "legacy")
                else:
                    outbox.mark_unknown(legacy)
                client = XhhClient("user_pkey=abc", outbox=outbox)
                client.request = mock.AsyncMock(
                    return_value={"status": "ok", "result": {"link_id": "new"}}
                )
                if status == "send_unknown":
                    with self.assertRaisesRegex(XhhError, "待对账"):
                        asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
                    client.request.assert_not_awaited()
                else:
                    result = asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
                    self.assertEqual(result["duplicate"], not draft)
                    self.assertEqual(result["link_id"], "new" if draft else "legacy")

    def test_unknown_attempt_blocks_retry_without_losing_metadata(self):
        outbox = self._outbox()
        client = XhhClient("user_pkey=abc", outbox=outbox)
        client.request = mock.AsyncMock(side_effect=NetworkError("response lost"))
        with self.assertRaises(NetworkError):
            asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
        before = outbox.unknown_rows()
        with self.assertRaisesRegex(XhhError, "待对账"):
            asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
        self.assertEqual(outbox.unknown_rows(), before)
        self.assertEqual(client.request.await_count, 1)

    def test_concurrent_publish_sends_only_once(self):
        outbox = self._outbox()
        client = XhhClient("user_pkey=abc", outbox=outbox)

        async def run():
            entered = asyncio.Event()
            release = asyncio.Event()

            async def request(*args, **kwargs):
                entered.set()
                await release.wait()
                return {"status": "ok", "result": {"link_id": "1"}}

            client.request = mock.AsyncMock(side_effect=request)
            first = asyncio.create_task(client.publish("标题", "正文", topic_ids=["43"]))
            await asyncio.wait_for(entered.wait(), timeout=2)
            try:
                with self.assertRaisesRegex(XhhError, "待对账"):
                    await client.publish("标题", "正文", topic_ids=["43"])
            finally:
                release.set()
                await first
            self.assertEqual(client.request.await_count, 1)

        asyncio.run(run())

    def test_cancelled_publish_keeps_recoverable_record(self):
        outbox = self._outbox()
        client = XhhClient("user_pkey=abc", outbox=outbox)

        async def run():
            entered = asyncio.Event()

            async def request(*args, **kwargs):
                entered.set()
                await asyncio.Future()

            client.request = request
            task = asyncio.create_task(client.publish("标题", "正文", topic_ids=["43"]))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        self.assertEqual(len(outbox.unknown_rows()), 1)
        self.assertEqual(outbox.pending_rows(), [])

    def test_reconcile_recovers_pending_but_skips_other_accounts_and_drafts(self):
        outbox = self._outbox()
        client = XhhClient("user_pkey=abc", heybox_id="123", outbox=outbox)
        for fingerprint, account, form in (
            ("interrupted", client._account_key, {}),
            ("other", "id:456", {}),
            ("draft", client._account_key, {"draft": "1"}),
        ):
            outbox.mark_pending(fingerprint, {
                "title": "标题", "account": account, "form": form,
                "baseline_ids": [], "posted_at": time.time(),
            })
        client._reconcile_link_id = mock.AsyncMock(return_value="recovered")
        self.assertEqual(asyncio.run(client.reconcile_outbox()), 1)
        self.assertEqual(client._reconcile_link_id.await_count, 1)
        self.assertEqual({row[0] for row in outbox.unknown_rows()}, {"other", "draft"})

    def test_ambiguous_http_responses_keep_unknown(self):
        cases = [
            (502, {"error": "gateway"}),
            (200, None),
            (200, []),
            (200, {"error": "permission denied", "code": 403}),
            (200, {"status": []}),
            (200, {"status": "unexpected"}),
            (200, {"status": "ok", "result": []}),
        ]
        for code, payload in cases:
            with self.subTest(code=code, payload=payload):
                outbox = self._outbox()
                client = XhhClient("user_pkey=abc", outbox=outbox)
                response = FakeResponse(payload, code)
                if payload is None:
                    response._raw = b"<html>connection lost</html>"
                with mock.patch("urllib.request.urlopen", return_value=response):
                    with self.assertRaises(NetworkError):
                        asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
                self.assertEqual(len(outbox.unknown_rows()), 1)

    def test_interrupted_response_body_keeps_unknown(self):
        outbox = self._outbox()
        client = XhhClient("user_pkey=abc", outbox=outbox)
        response = FakeResponse({})
        response.read = mock.Mock(side_effect=IncompleteRead(b"partial response"))
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(NetworkError):
                asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
        self.assertEqual(len(outbox.unknown_rows()), 1)

    def test_unexpected_request_failure_keeps_unknown(self):
        outbox = self._outbox()
        client = XhhClient("user_pkey=abc", outbox=outbox)
        client.request = mock.AsyncMock(side_effect=RuntimeError("unexpected response failure"))
        with self.assertRaises(RuntimeError):
            asyncio.run(client.publish("标题", "正文", topic_ids=["43"]))
        self.assertEqual(len(outbox.unknown_rows()), 1)

    def test_http_error_with_success_body_cannot_delete_ledger(self):
        outbox = self._outbox()
        outbox.mark_pending("fp", {})
        outbox.mark_sent("fp", "123")
        client = XhhClient("user_pkey=abc", outbox=outbox)
        error = urllib.error.HTTPError(
            "https://api.xiaoheihe.cn/test", 403, "Forbidden", {},
            io.BytesIO(b'{"status":"ok","result":{}}'),
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(XhhError):
                asyncio.run(client.delete_link("123"))
        self.assertEqual(outbox.remove_by_link_id("123"), 1)

    def test_missing_status_cannot_delete_ledger(self):
        outbox = self._outbox()
        outbox.mark_pending("fp", {})
        outbox.mark_sent("fp", "123")
        client = XhhClient("user_pkey=abc", outbox=outbox)
        client.request = mock.AsyncMock(return_value={"error": "denied", "code": 403})
        with self.assertRaises(NetworkError):
            asyncio.run(client.delete_link("123"))
        self.assertEqual(outbox.remove_by_link_id("123"), 1)


if __name__ == "__main__":
    unittest.main()
