"""统一发布服务的离线集成测试；请求全部由模拟客户端处理。"""

import asyncio
import copy
import pathlib
import shutil
import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))
from adapter_tests.astrbot_stubs import install

install()

from astrbot_plugin_heybox_poster.core.client import NetworkError, RateLimited
from astrbot_plugin_heybox_poster.core.outbox import Outbox
from astrbot_plugin_heybox_poster.core.publishing import PublishingService
from astrbot_plugin_heybox_poster.xiaoheihe.config_service import DEFAULT_CONFIG, ConfigService, ConfigValidationError
from astrbot_plugin_heybox_poster.xiaoheihe.models import Credentials, Notification, NotificationType, RoutingTarget, SendResult
from astrbot_plugin_heybox_poster.xiaoheihe.runtime import RuntimeServices
from astrbot_plugin_heybox_poster.xiaoheihe.api_client import XiaoheiheApiClient
from astrbot_plugin_heybox_poster.xiaoheihe.adapter import XiaoheihePlatformAdapter
from astrbot_plugin_heybox_poster.xiaoheihe.runtime import bind_runtime, unbind_runtime


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
        result = await self.service.approve(item["id"])
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["policy_blocked"])
        self.assertEqual(result["reason"], "reply_account_changed")
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

    async def test_config_redacts_unknown_credentials_and_rejects_unsafe_flags(self):
        self.config["cookie"] = "legacy-secret"
        self.assertNotIn("legacy-secret", str(self.runtime.config.snapshot()))
        self.assertNotIn("cookie", self.runtime.config.ui_schema())
        for field in ("dry_run", "post_review_required", "comment_review_required"):
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["publishing"][field] = "false"
            with self.assertRaises(ConfigValidationError):
                ConfigService(config)

    def comment_client(self, error=None):
        client = Mock(credentials=self.credentials, send_comment=AsyncMock(return_value=SendResult("88", True)))
        async def send(route, text, *, before_send):
            await before_send()
            if error:
                raise error
            return SendResult("88", True)
        client.send_comment.side_effect = send
        self.runtime.get_client = AsyncMock(return_value=client)
        return client

    async def test_comment_limit_is_shared_between_profiles_and_sources(self):
        self.config["publishing"]["comment_daily_limit"] = 1
        self.config["proactive_feed"]["dry_run"] = False
        self.config["profiles"].append(dict(self.config["profiles"][0], profile_id="second"))
        self.runtime.credentials.save(Credentials("second", "100", "同账号", self.credentials.cookies))
        client = self.comment_client()
        first = (await self.service.submit("comment", "default", dict(post_id="99", text="第一条")))["review"]
        self.assertEqual((await self.service.approve(first["id"]))["status"], "sent")
        second = (await self.service.submit_comment(RoutingTarget("second", "98"), "第二条", proactive=True))["review"]
        result = await self.service.approve(second["id"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "comment_account_daily_limit"))
        self.assertEqual(self.service.queue.get(second["id"])["status"], "pending")
        self.assertEqual((await self.runtime.repository.reply_budget_status("100"))["comment_count"], 1)
        client.send_comment.assert_awaited_once()

    async def test_concurrent_comment_approvals_cannot_take_last_slot_twice(self):
        self.config["publishing"]["comment_daily_limit"] = 1
        client = self.comment_client()
        reviews = [(await self.service.submit("comment", "default", dict(post_id=post, text=post)))["review"] for post in ("98", "99")]
        results = await asyncio.gather(*(self.service.approve(row["id"]) for row in reviews))
        self.assertEqual(sorted(result["status"] for result in results), ["pending", "sent"])
        client.send_comment.assert_awaited_once()

    async def test_local_stop_after_reservation_releases_quota_and_own_cooldown(self):
        self.config["publishing"]["cooldown_seconds"] = 60
        client = self.comment_client()
        row = (await self.service.submit("comment", "default", dict(post_id="99", text="回复")))["review"]
        reserve = self.runtime.repository.reserve_comment_attempt
        async def enable_simulation(*args, **kwargs):
            result = await reserve(*args, **kwargs)
            self.config["publishing"]["dry_run"] = True
            return result
        self.runtime.repository.reserve_comment_attempt = enable_simulation
        result = await self.service.approve(row["id"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "reply_dry_run_changed"))
        self.assertEqual((await self.runtime.repository.reply_budget_status("100"))["comment_count"], 0)
        self.assertEqual(self.outbox.cooldown_until("id:100:publishing"), 0)
        client.send_comment.assert_not_called()
        self.config["publishing"]["dry_run"] = False
        self.runtime.repository.reserve_comment_attempt = reserve
        self.assertEqual((await self.service.approve(row["id"]))["status"], "sent")
        client.send_comment.assert_awaited_once()

    async def test_cancel_during_final_local_check_is_pending_but_network_cancel_is_unknown(self):
        client = self.comment_client()
        row = (await self.service.submit("comment", "default", dict(post_id="99", text="回复")))["review"]
        check = self.service.check_comment_policy
        calls = 0
        async def cancel_last_check(item):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise asyncio.CancelledError()
            await check(item)
        self.service.check_comment_policy = cancel_last_check
        with self.assertRaises(asyncio.CancelledError):
            await self.service.approve(row["id"])
        self.assertEqual(self.service.queue.get(row["id"])["status"], "pending")
        self.assertEqual((await self.runtime.repository.reply_budget_status("100"))["comment_count"], 0)
        client.send_comment.assert_not_called()
        self.service.check_comment_policy = check
        async def cancel_network(*args, before_send):
            await before_send()
            raise asyncio.CancelledError()
        client.send_comment.side_effect = cancel_network
        with self.assertRaises(asyncio.CancelledError):
            await self.service.approve(row["id"])
        self.assertEqual(self.service.queue.get(row["id"])["status"], "send_unknown")
        self.assertEqual((await self.runtime.repository.reply_budget_status("100"))["comment_count"], 1)

    async def test_cancel_before_attempt_registration_returns_comment_to_pending(self):
        self.runtime.get_client = AsyncMock(side_effect=asyncio.CancelledError())
        row = (await self.service.submit("comment", "default", dict(post_id="99", text="回复")))["review"]
        with self.assertRaises(asyncio.CancelledError):
            await self.service.approve(row["id"])
        self.assertEqual(self.service.queue.get(row["id"])["status"], "pending")
        self.assertEqual((await self.runtime.repository.reply_budget_status("100"))["comment_count"], 0)

    async def shared_notification_review(self):
        payload = dict(post_id="99", parent_comment_id="77", root_comment_id="77", text="同一回复")
        events = []
        for index in (1, 2):
            notice = Notification(
                profile_id="default", external_event_id=str(index), external_comment_id=str(index),
                notification_id=str(index), event_type=NotificationType.MENTION,
                sender_uid=str(200 + index), sender_nickname="用户", post_id="99",
                root_comment_id=str(index), parent_comment_id=str(index), content="你好", created_at=1,
            )
            event_id = await self.runtime.repository.claim_event(notice)
            await self.runtime.repository.reserve_reply_admission(
                event_id, notice, account_id="100", thread_once=True, user_daily_limit=5,
                account_daily_limit=30, owner_exempt=False,
            )
            context = await self.service._source_context("default", event_id)
            row = self.service.queue.submit(
                "comment", "default", "100", payload, source="notification",
                source_id=str(event_id), source_context=context,
            )
            events.append(event_id)
        await self.service.settle_sources(row)
        return row, events

    async def test_every_notification_source_is_checked_and_rejection_releases_all_floors(self):
        client = self.comment_client()
        row, events = await self.shared_notification_review()
        self.assertEqual(len(self.service.queue.sources(row["id"])), 2)
        self.config["permissions"]["user_blacklist"] = ["202"]
        result = await self.service.approve(row["id"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "reply_policy_changed"))
        client.send_comment.assert_not_called()
        await self.service.reject(row["id"])
        admissions = await self.runtime.repository.db.fetchall(
            "SELECT floor_active,outcome FROM reply_admissions ORDER BY incoming_event_id",
        )
        self.assertEqual([(item["floor_active"], item["outcome"]) for item in admissions], [(0, "rejected"), (0, "rejected")])
        self.assertEqual((await self.runtime.repository.reply_budget_status("100"))["processing_count"], 2)
        for event_id in events:
            event = await self.runtime.repository.db.fetchone("SELECT status FROM incoming_events WHERE id=?", (event_id,))
            self.assertEqual(event["status"], "ignored")

    async def test_reconcile_confirms_existing_send_and_all_notification_sources_once(self):
        row, events = await self.shared_notification_review()
        self.service.queue.claim(row["id"], profile_id="default", account_id="100")
        self.service.queue.finish(row["id"], "send_unknown")
        route = RoutingTarget("default", "99", "77", "77")
        reservation = await self.runtime.repository.reserve_comment_attempt(
            "default", events[0], route, "同一回复", account_id="100",
            send_key=f"review:{row['id']}", daily_limit=30,
        )
        await self.runtime.repository.confirm_outgoing(reservation["id"], "88")
        await self.runtime.repository.db.execute("UPDATE incoming_events SET status='send_unknown' WHERE id IN (?,?)", tuple(events))
        await self.service.reconcile()
        await self.service.reconcile()
        self.assertEqual(self.service.queue.get(row["id"])["status"], "sent")
        rows = await self.runtime.repository.db.fetchall("SELECT status FROM incoming_events WHERE id IN (?,?)", tuple(events))
        self.assertEqual([item["status"] for item in rows], ["sent", "sent"])
        self.assertEqual((await self.runtime.repository.reply_budget_status("100"))["comment_count"], 1)

    async def test_source_added_during_policy_check_requires_new_approval(self):
        client = self.comment_client()
        row, _ = await self.shared_notification_review()
        original = self.runtime.repository.risk_user

        async def attach_source(*args, **kwargs):
            self.service.queue.submit(
                "comment", "default", "100", row["payload"],
                source="notification", source_id="9999", source_context={},
            )
            return await original(*args, **kwargs)

        self.runtime.repository.risk_user = attach_source
        result = await self.service.approve(row["id"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "reply_policy_changed"))
        client.send_comment.assert_not_called()
        self.assertEqual((await self.runtime.repository.reply_budget_status("100"))["comment_count"], 0)

    async def test_old_generation_callbacks_cannot_change_a_new_admission(self):
        notice = Notification(
            profile_id="default", external_event_id="1", external_comment_id="1",
            notification_id="1", event_type=NotificationType.MENTION, sender_uid="201",
            sender_nickname="用户", post_id="99", root_comment_id="77", parent_comment_id="77",
            content="你好", created_at=1,
        )
        repo = self.runtime.repository
        event_id = await repo.claim_event(notice)
        kwargs = dict(account_id="100", thread_once=True, user_daily_limit=5, account_daily_limit=30, owner_exempt=False)
        first = await repo.reserve_reply_admission(event_id, notice, **kwargs)
        await repo.schedule_retry(event_id, "上下文暂不可用", max_retries=3)
        await repo.db.execute("UPDATE incoming_events SET next_retry_at=0 WHERE id=?", (event_id,))
        self.assertEqual(await repo.claim_retry_event(notice), event_id)
        second = await repo.reserve_reply_admission(event_id, notice, **kwargs)
        self.assertTrue(second["created"])
        await self.runtime.fail_reply(event_id=event_id, route=notice.route, error="旧回调", generated_ms=1, admission_id=first["id"])
        await self.runtime.expire_reply(event_id=event_id, route=notice.route, generated_ms=1000, reply_timeout_seconds=1, admission_id=first["id"])
        with self.assertRaisesRegex(ValueError, "尝试已变更"):
            await self.runtime.deliver(event_id=event_id, route=notice.route, content="旧回复", dry_run=False, admission_id=first["id"])
        row = await repo.db.fetchone("SELECT status FROM incoming_events WHERE id=?", (event_id,))
        admission = await repo.db.fetchone("SELECT outcome,floor_active FROM reply_admissions WHERE id=?", (second["id"],))
        self.assertEqual((row["status"], admission["outcome"], admission["floor_active"]), ("claimed", "processing", 1))
        self.assertFalse(self.service.queue.list())

    async def test_manual_user_status_is_rechecked_for_all_sources_after_body_cleanup(self):
        client = self.comment_client()
        row, events = await self.shared_notification_review()
        await self.runtime.repository.db.execute(
            "UPDATE incoming_events SET content=NULL,raw_json=NULL WHERE id IN (?,?)", tuple(events),
        )
        await self.runtime.repository.manage_risk_user(
            "default", "202", action="block", note="人工暂停", operator="admin",
            risk_config=self.config["risk"],
        )
        self.config["risk"]["enabled"] = False
        result = await self.service.approve(row["id"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "risk_manual_blocked"))
        self.assertIn("user_filter:default:202", self.runtime._alerts)
        client.send_comment.assert_not_called()
        await self.runtime.repository.manage_risk_user(
            "default", "202", action="unblock", note="恢复", operator="admin",
            risk_config=self.config["risk"],
        )
        self.assertEqual((await self.service.approve(row["id"]))["status"], "sent")
        self.assertNotIn("user_filter:default:202", self.runtime._alerts)
        client.send_comment.assert_awaited_once()

    def test_user_filter_alerts_log_only_changes(self):
        emit = self.runtime.logging.emit = Mock()
        self.runtime.record_user_filter("default", "201", "risk_manual_blocked")
        self.runtime.record_user_filter("default", "201", "risk_manual_blocked")
        self.assertEqual(emit.call_count, 1)
        self.runtime.record_user_filter("default", "201", "")
        self.runtime.record_user_filter("default", "201", "")
        self.assertEqual(emit.call_count, 2)
        self.assertNotIn("user_filter:default:201", self.runtime._alerts)

    async def test_missing_source_is_pending_while_verified_link_without_uid_can_send(self):
        client = self.comment_client()
        missing = (await self.service.submit_comment(RoutingTarget("default", "99"), "回复", event_id=42))["review"]
        result = await self.service.approve(missing["id"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "risk_source_unresolved"))
        client.send_comment.assert_not_called()
        notice = Notification(
            profile_id="default", external_event_id="2", external_comment_id="2",
            notification_id="2", event_type=NotificationType.MENTION, sender_uid="",
            sender_nickname="未知用户", post_id="98", root_comment_id="77", parent_comment_id="77",
            content="你好", created_at=1,
        )
        event_id = await self.runtime.repository.claim_event(notice)
        await self.runtime.repository.reserve_reply_admission(
            event_id, notice, account_id="100", thread_once=True, user_daily_limit=5,
            account_daily_limit=30, owner_exempt=False,
        )
        item = (await self.service.submit_comment(notice.route, "回复", event_id=event_id))["review"]
        self.assertEqual((await self.service.approve(item["id"]))["status"], "sent")
        client.send_comment.assert_awaited_once()

    async def test_final_check_runs_after_client_rate_wait_before_any_http_write(self):
        requests = []
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"status": "ok", "commentid": "88"}),
        ), base_url="https://api.xiaoheihe.cn")
        self.addAsyncCleanup(http_client.aclose)
        client = XiaoheiheApiClient("default", credentials=self.credentials, client=http_client)
        self.runtime.get_client = AsyncMock(return_value=client)
        self.config["publishing"]["cooldown_seconds"] = 60
        row = (await self.service.submit("comment", "default", dict(post_id="99", text="回复")))["review"]
        async def change_to_simulation():
            self.config["publishing"]["dry_run"] = True
        client._limiter = SimpleNamespace(wait=change_to_simulation)
        result = await self.service.approve(row["id"])
        self.assertEqual((result["status"], result["reason"]), ("pending", "reply_dry_run_changed"))
        self.assertEqual(requests, [])
        self.assertEqual((await self.runtime.repository.reply_budget_status("100"))["comment_count"], 0)
        self.assertEqual(self.outbox.cooldown_until("id:100:publishing"), 0)
        self.config["publishing"]["dry_run"] = False
        async def cancel_rate_wait():
            raise asyncio.CancelledError()
        client._limiter = SimpleNamespace(wait=cancel_rate_wait)
        with self.assertRaises(asyncio.CancelledError):
            await self.service.approve(row["id"])
        self.assertEqual(self.service.queue.get(row["id"])["status"], "pending")
        self.assertEqual(requests, [])
        self.assertEqual((await self.runtime.repository.reply_budget_status("100"))["comment_count"], 0)
        client._limiter = SimpleNamespace(wait=AsyncMock())
        self.assertEqual((await self.service.approve(row["id"]))["status"], "sent")
        self.assertEqual(len(requests), 1)

    async def test_proactive_context_cannot_rebind_when_account_changes(self):
        bind_runtime(self.runtime)
        self.addCleanup(unbind_runtime, self.runtime)
        adapter = XiaoheihePlatformAdapter({"id": "test", "profile_id": "default"}, {}, asyncio.Queue())
        self.comment_client()
        async def switch_account(*args):
            self.runtime.credentials.save(Credentials("default", "200", "新账号", self.credentials.cookies))
            return Mock()
        adapter._context_builder = Mock(build=AsyncMock(side_effect=switch_account))
        adapter.commit_event = Mock()
        notice = Notification(
            profile_id="default", external_event_id="feed:99", external_comment_id="feed:99",
            notification_id="feed:99", event_type=NotificationType.PROACTIVE_FEED,
            sender_uid="201", sender_nickname="作者", post_id="99", root_comment_id="",
            parent_comment_id="", content="帖子", created_at=1,
        )
        with self.assertRaisesRegex(RuntimeError, "账号已切换"):
            await adapter._dispatch_synthetic(notice, {})
        row = await self.runtime.repository.db.fetchone("SELECT reply_account_id FROM incoming_events WHERE external_event_id='feed:99'")
        self.assertEqual(row["reply_account_id"], "100")
        adapter.commit_event.assert_not_called()
        self.assertFalse(self.service.queue.list())

    async def test_utc_day_change_during_final_policy_check_keeps_pending_without_http(self):
        requests = []
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"status": "ok", "commentid": "88"}),
        ), base_url="https://api.xiaoheihe.cn")
        self.addAsyncCleanup(http_client.aclose)
        client = XiaoheiheApiClient("default", credentials=self.credentials, client=http_client)
        client._limiter = SimpleNamespace(wait=AsyncMock())
        self.runtime.get_client = AsyncMock(return_value=client)
        self.config["publishing"].update(comment_daily_limit=1, cooldown_seconds=60)
        row = (await self.service.submit("comment", "default", dict(post_id="99", text="回复")))["review"]
        current_time = 1793491199.0
        clock = SimpleNamespace(time=lambda: current_time)
        original = self.service.check_comment_policy
        checks = 0

        async def cross_midnight(review):
            nonlocal current_time, checks
            await original(review)
            checks += 1
            if checks == 4:
                current_time += 1
                # Another operation consumes today's last slot while this check awaits.
                await self.runtime.repository.reserve_comment_attempt(
                    "default", None, RoutingTarget("default", "98"), "其他回复",
                    account_id="100", send_key="other-review", daily_limit=1,
                )

        self.service.check_comment_policy = cross_midnight
        with patch("astrbot_plugin_heybox_poster.xiaoheihe.runtime.time", clock), patch(
            "astrbot_plugin_heybox_poster.xiaoheihe.repository.time", clock,
        ):
            result = await self.service.approve(row["id"])
            self.assertEqual((result["status"], result["reason"]), ("pending", "reply_policy_changed"))
            self.assertEqual(requests, [])
            self.assertEqual((await self.runtime.repository.reply_budget_status("100"))["comment_count"], 1)
            outgoing = await self.runtime.repository.db.fetchone(
                "SELECT status FROM outgoing_replies WHERE send_key=?", (f"review:{row['id']}",),
            )
            self.assertEqual(outgoing["status"], "retry_wait")
            self.assertEqual(self.outbox.cooldown_until("id:100:publishing"), 0)

    async def test_successful_review_history_cannot_restore_expired_floor(self):
        row, events = await self.shared_notification_review()
        self.comment_client()
        self.assertEqual((await self.service.approve(row["id"]))["status"], "sent")
        complete = await self.runtime.repository.db.fetchall("SELECT completed_at FROM incoming_events")
        self.assertTrue(all(item["completed_at"] is not None for item in complete))
        await self.runtime.repository.db.execute("UPDATE incoming_events SET completed_at=1")
        await self.runtime.repository.db.execute("DELETE FROM reply_admissions")
        await self.service.reconcile()
        self.assertEqual((await self.runtime.repository.table_counts())["reply_admissions"], 0)
