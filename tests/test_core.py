"""签名与发帖表单的离线测试（不联网、不需要 AstrBot）。

运行：``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import asyncio
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


if __name__ == "__main__":
    unittest.main()
