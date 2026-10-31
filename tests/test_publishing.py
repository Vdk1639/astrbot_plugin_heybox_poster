"""统一发布服务的离线集成测试；请求全部由模拟客户端处理。"""

import asyncio
import copy
import pathlib
import shutil
import sys
import unittest
import uuid
from unittest.mock import AsyncMock, Mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_heybox_poster.core.client import NetworkError, RateLimited
from astrbot_plugin_heybox_poster.core.outbox import Outbox
from astrbot_plugin_heybox_poster.core.publishing import PublishingService
from astrbot_plugin_heybox_poster.xiaoheihe.config_service import DEFAULT_CONFIG, ConfigService, ConfigValidationError
from astrbot_plugin_heybox_poster.xiaoheihe.models import Credentials, RoutingTarget, SendResult
from astrbot_plugin_heybox_poster.xiaoheihe.runtime import RuntimeServices


class PublishingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = ROOT / ".test-runs" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config["publishing"].update(dry_run=False, cooldown_seconds=0)
        self.config["profiles"][0]["dry_run"] = False
        self.runtime = RuntimeServices(self.config, self.directory)
        await self.runtime.database.open()
        self.runtime._started = True
        self.addAsyncCleanup(self.runtime.close)
        self.credentials = Credentials("default", "100", "测试", {"user_pkey": "p", "x_xhh_tokenid": "t"})
        self.runtime.credentials.save(self.credentials)
        self.outbox = Outbox(":memory:")
        self.addCleanup(self.outbox.close)
        self.post_client = Mock(publish=AsyncMock(return_value={"link_id": "99", "raw": {"cookie": "secret"}}))
        self.client_factory = Mock(return_value=self.post_client)
        self.service = PublishingService(self.runtime, self.client_factory, lambda: self.outbox)
        self.runtime.publisher = self.service
        self.addCleanup(self.service.close)
        self.post = dict(title="标题", text="正文", topic_ids=["43"], mode="article", draft=False, view_limit=3)

    async def test_posts_require_review_and_keep_payload_and_identity(self):
        submitted = await self.service.submit("post", "default", self.post)
        self.assertEqual(submitted["status"], "pending")
        self.post_client.publish.assert_not_called()
        result = await self.service.approve(submitted["review"]["id"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["review"]["account_id"], "100")
        self.post_client.publish.assert_awaited_once_with("标题", "正文", topic_ids=["43"], mode="article", draft=False, view_limit=3)
        self.assertNotIn("secret", str(result))
        with self.assertRaises(ValueError):
            await self.service.approve(submitted["review"]["id"])

    async def test_concurrent_approvals_send_once(self):
        item = (await self.service.submit("post", "default", self.post))["review"]
        replies = await asyncio.gather(self.service.approve(item["id"]), self.service.approve(item["id"]), return_exceptions=True)
        self.assertEqual(sum(isinstance(x, ValueError) for x in replies), 1)
        self.post_client.publish.assert_awaited_once()

    async def test_account_switch_blocks_pending_item(self):
        item = (await self.service.submit("post", "default", self.post))["review"]
        self.runtime.credentials.save(Credentials("default", "200", "新账号", self.credentials.cookies))
        with self.assertRaisesRegex(ValueError, "账号已切换"):
            await self.service.approve(item["id"])
        self.assertEqual(self.service.queue.get(item["id"])["status"], "pending")
        self.post_client.publish.assert_not_called()

    async def test_selected_profile_is_used_for_post_and_comment(self):
        self.config["profiles"].append(dict(self.config["profiles"][0], profile_id="second"))
        other = Credentials("second", "200", "第二账号", self.credentials.cookies)
        self.runtime.credentials.save(other)
        item = (await self.service.submit("post", "second", self.post))["review"]
        await self.service.approve(item["id"])
        self.client_factory.assert_called_with("second")
        client = Mock(credentials=other, send_comment=AsyncMock(return_value=SendResult("88", True)))
        self.runtime.get_client = AsyncMock(return_value=client)
        comment = (await self.service.submit("comment", "second", dict(post_id="99", text="回复", parent_comment_id="77", root_comment_id="66")))["review"]
        result = await self.service.approve(comment["id"])
        self.assertEqual(result["status"], "sent")
        route, text = client.send_comment.call_args.args
        self.assertEqual((route.profile_id, route.parent_comment_id, route.root_comment_id, text), ("second", "77", "66", "回复"))

    async def test_all_dry_run_gates_leave_approval_pending(self):
        for group in ("publishing", "profile", "proactive_feed"):
            with self.subTest(group=group):
                config = self.config["profiles"][0] if group == "profile" else self.config[group]
                config["dry_run"] = True
                if group == "proactive_feed":
                    item = (await self.service.submit_comment(RoutingTarget("default", "99"), "评论", proactive=True))["review"]
                else:
                    item = (await self.service.submit("post", "default", self.post))["review"]
                self.assertEqual((await self.service.approve(item["id"]))["status"], "dry_run")
                self.assertEqual(self.service.queue.get(item["id"])["status"], "pending")
                config["dry_run"] = False
        self.post_client.publish.assert_not_called()

    async def test_disabled_or_logged_out_profile_cannot_approve(self):
        item = (await self.service.submit("post", "default", self.post))["review"]
        self.config["profiles"][0]["enabled"] = False
        with self.assertRaisesRegex(ValueError, "停用"):
            await self.service.approve(item["id"])
        self.config["profiles"][0]["enabled"] = True
        self.runtime.credentials.delete("default")
        with self.assertRaisesRegex(ValueError, "尚未登录"):
            await self.service.approve(item["id"])

    async def test_native_comments_are_queued_without_network_and_reject_is_final(self):
        result = await self.service.submit_comment(RoutingTarget("default", "99"), "评论", event_id=42, expected_account_id="100")
        self.assertEqual(result["status"], "pending")
        await self.service.reject(result["review"]["id"])
        repeated = await self.service.submit_comment(RoutingTarget("default", "99"), "重新生成", event_id=42)
        self.assertEqual(repeated["status"], "rejected")
        self.client_factory.assert_not_called()

    async def test_native_account_switch_during_generation_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "生成回复期间"):
            await self.service.submit_comment(RoutingTarget("default", "99"), "评论", expected_account_id="other")
        self.assertFalse(self.service.queue.list())

    async def test_account_switch_while_comment_client_is_acquired_is_blocked(self):
        client = Mock(credentials=self.credentials, send_comment=AsyncMock())
        async def switch(*args, **kwargs):
            self.runtime.credentials.save(Credentials("default", "200", "新账号", self.credentials.cookies))
            return client
        self.runtime.get_client = switch
        item = (await self.service.submit("comment", "default", dict(post_id="99", text="回复")))["review"]
        self.assertEqual((await self.service.approve(item["id"]))["status"], "failed")
        client.send_comment.assert_not_called()

    async def test_send_uncertainty_and_cancellation_never_allow_reapproval(self):
        for error in (NetworkError("secret server payload"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                payload = dict(self.post, text=type(error).__name__)
                item = (await self.service.submit("post", "default", payload))["review"]
                self.post_client.publish.side_effect = error
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.service.approve(item["id"])
                else:
                    await self.service.approve(item["id"])
                row = self.service.queue.get(item["id"])
                self.assertEqual(row["status"], "send_unknown")
                self.assertNotIn("secret", row["error"])
                with self.assertRaises(ValueError):
                    await self.service.approve(item["id"])
                self.assertEqual((await self.service.submit("post", "default", payload))["status"], "send_unknown")

    async def test_missing_post_id_is_unknown_including_drafts(self):
        self.post_client.publish.return_value = {}
        for draft in (False, True):
            item = (await self.service.submit("post", "default", dict(self.post, draft=draft)))["review"]
            self.assertEqual((await self.service.approve(item["id"]))["status"], "send_unknown")

    async def test_rate_limit_blocks_other_submissions_for_same_account(self):
        self.post_client.publish.side_effect = RateLimited("limited")
        self.config["publishing"]["post_review_required"] = False
        self.assertEqual((await self.service.submit("post", "default", self.post))["status"], "failed")
        with self.assertRaisesRegex(ValueError, "冷却"):
            await self.service.submit("post", "default", dict(self.post, text="另一条"))
        self.post_client.publish.assert_awaited_once()

    async def test_explicit_direct_mode_still_uses_dedup_queue(self):
        self.config["publishing"]["post_review_required"] = False
        self.assertEqual((await self.service.submit("post", "default", self.post))["status"], "sent")
        self.assertEqual((await self.service.submit("post", "default", self.post))["status"], "sent")
        self.post_client.publish.assert_awaited_once()

    async def test_boundary_validation_precedes_writes(self):
        for patch in ({"topic_ids": "43"}, {"draft": "false"}, {"mode": "invalid"}, {"view_limit": True}, {"cookie": "secret"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                await self.service.submit("post", "default", dict(self.post, **patch))
        for payload in ({"post_id": "../1", "text": "回复"}, {"post_id": "99", "text": ""}, {"post_id": "99", "text": []}):
            with self.assertRaises(ValueError):
                await self.service.submit("comment", "default", payload)
        self.assertFalse(self.service.queue.list())

    async def test_config_redacts_legacy_credentials_and_rejects_unsafe_flags(self):
        self.config["cookie"] = "legacy-secret"
        self.assertNotIn("legacy-secret", str(self.runtime.config.snapshot()))
        self.assertNotIn("cookie", self.runtime.config.ui_schema())
        for field in ("dry_run", "post_review_required", "comment_review_required"):
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["publishing"][field] = "false"
            with self.assertRaises(ConfigValidationError):
                ConfigService(config)
