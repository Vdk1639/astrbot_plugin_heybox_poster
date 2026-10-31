"""不装 AstrBot 也能跑的插件冒烟测试：用桩模块替代 ``astrbot``。

使用真实临时 runtime 验证账号装配、权限、发布审核、频控和原生 Agent 转发。
AstrBot 使用共享桩；所有网络入口均被测试守卫禁止。
"""

from __future__ import annotations

import ast
import asyncio
import time
import unittest
from unittest import mock

from plugin_test_support import (
    ROOT, Context, FakeConfig, PluginTestCase, SmokeEvent as AstrMessageEvent, XhhPosterPlugin,
)
from astrbot_plugin_heybox_poster.core.client import (
    CaptchaRequired, NetworkError, RateLimited, XhhError,
)


class FakeClient:
    async def search_topics(self, keyword, limit=5):
        return [{"topic_id": "43", "name": "Steam"}] if keyword == "Steam" else []

class PluginSmokeTests(PluginTestCase):
    def test_command_args_accepts_both_prefix_forms(self):
        plugin = self._plugin()
        for message in ("/xhh话题 Steam", "xhh话题 Steam", "Steam"):
            with self.subTest(message=message):
                self.assertEqual(plugin._command_args(AstrMessageEvent(message=message), "xhh话题"), "Steam")

    async def test_account_config_change_rebuilds_client(self):
        plugin = self._plugin()
        plugin._get_outbox = lambda: None

        async def run():
            first = plugin._get_client()
            self.assertIs(plugin._get_client(), first)
            self._set_credentials(plugin, uid="2")
            second = plugin._get_client()
            self.assertIsNot(first, second)
            self.assertNotEqual(first._account_key, second._account_key)
            self._set_credentials(plugin, uid="2", cookie="new")
            third = plugin._get_client()
            self.assertIsNot(second, third)
            self.assertEqual(third.cookie, plugin._cookie())
            await plugin.terminate()

        await run()

    def test_whitelist(self):
        plugin = self._plugin(allowed_users=["10001"])

        self.assertTrue(plugin._is_allowed(AstrMessageEvent("10001")))
        self.assertFalse(plugin._is_allowed(AstrMessageEvent("20002")))

    def test_platform_prefixes_isolate_same_sender_ids(self):
        plugin = self._plugin(allowed_users=["qq:123", "xhh:456"])
        for sender, platform, expected in (
            ("123", "aiocqhttp", True), ("123", "qq_official", True),
            ("123", "xiaoheihe", False), ("456", "heybox", True),
            ("456", "heybox_community", True), ("123", "heybox_community", False),
            ("456", "aiocqhttp", False), ("123", "discord", False),
        ):
            with self.subTest(sender=sender, platform=platform):
                event = AstrMessageEvent(sender, platform=platform)
                self.assertEqual(plugin._is_allowed(event), expected)

    def test_bare_whitelist_ids_keep_current_platform_compatibility(self):
        plugin = self._plugin(allowed_users=["123"])
        for platform in ("aiocqhttp", "xiaoheihe", "heybox_community"):
            self.assertTrue(plugin._is_allowed(AstrMessageEvent("123", platform=platform)))

    def test_platform_cooldowns_and_locks_are_separate(self):
        plugin = self._plugin()
        qq = AstrMessageEvent("123", platform="aiocqhttp")
        xhh = AstrMessageEvent("123", platform="xiaoheihe")
        self.assertEqual(plugin._check_cooldown(qq), "")
        self.assertEqual(plugin._check_cooldown(xhh), "")
        self.assertIsNot(plugin._lock_for(plugin._sender(qq)), plugin._lock_for(plugin._sender(xhh)))

    def test_empty_whitelist_admin_role_allowed(self):
        plugin = self._plugin()

        self.assertTrue(plugin._is_allowed(AstrMessageEvent("10001", role="admin")))

    def test_empty_whitelist_non_admin_denied_when_admins_configured(self):
        plugin = self._plugin(context=Context({"admins_id": ["10001"]}))
        self.assertTrue(plugin._is_allowed(AstrMessageEvent("10001")))
        self.assertFalse(plugin._is_allowed(AstrMessageEvent("20002")))

    def test_empty_whitelist_denies_when_no_admin_source(self):
        plugin = self._plugin()

        self.assertFalse(plugin._is_allowed(AstrMessageEvent("anyone")))

    def test_admin_lookup_failure_denies_access(self):
        plugin = self._plugin(context=mock.Mock())
        plugin.context.get_config.side_effect = RuntimeError("unavailable")
        event = AstrMessageEvent("anyone")
        event.is_admin = mock.Mock(side_effect=RuntimeError("unavailable"))
        self.assertFalse(plugin._is_allowed(event))

    async def test_delete_obeys_dry_run(self):
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                plugin = self._plugin(allowed_users=["10001"], dry_run=dry_run)
                client = mock.Mock(delete_link=mock.AsyncMock(return_value={}))
                plugin._get_client = mock.Mock(return_value=client)
                event = AstrMessageEvent("10001", "/xhh删除 123")
                event.plain_result = str

                async def run():
                    return [reply async for reply in plugin.cmd_delete(event)]

                replies = await run()
                if dry_run:
                    plugin._get_client.assert_not_called()
                    self.assertIn("dry-run", replies[0])
                else:
                    client.delete_link.assert_awaited_once_with("123")

    async def test_missing_admin_denies_write_entrypoints(self):
        plugin = self._plugin()
        plugin._get_client = mock.Mock(side_effect=AssertionError("must not send"))
        event = AstrMessageEvent("outsider", "/xhh删除 123")
        event.plain_result = str

        async def run():
            self.assertIn("权限", await plugin.tool_publish(event, "标题", "正文", topic_ids="43"))
            for command in (plugin.cmd_post, plugin.cmd_delete):
                replies = [reply async for reply in command(event)]
                self.assertIn("权限", replies[0])

        await run()
        plugin._get_client.assert_not_called()

    async def test_missing_dry_run_defaults_to_preview_for_all_write_entrypoints(self):
        for value in ("missing", None):
            with self.subTest(value=value):
                settings = {
                    "post_review_required": False, "comment_review_required": False,
                    "default_topic_ids": "43", "cooldown_seconds": 0,
                }
                if value is None:
                    settings["dry_run"] = None
                plugin = self._plugin(raw_config=FakeConfig({"publishing": settings}))
                plugin._get_client = mock.Mock(side_effect=AssertionError("must not write"))
                event = AstrMessageEvent(role="admin", message="/xhh发帖 标题|正文|43")
                replies = [reply async for reply in plugin.cmd_post(event)]
                replies.append(await plugin.tool_publish(event, "标题", "正文", topic_ids="43"))
                replies.append(await plugin.tool_comment(event, "123", "评论"))
                event.message_str = "/xhh删除 123"
                replies.extend([reply async for reply in plugin.cmd_delete(event)])
                for reply in replies:
                    self.assertIn("dry-run", reply)
                plugin._get_client.assert_not_called()

    async def test_publish_entrypoints_preserve_success_duplicate_and_error_results(self):
        for command in (True, False):
            for result, expected in (
                ({"link_id": "123"}, "发布成功"),
                ({"link_id": "123", "duplicate": True}, "去重窗口"),
                (NetworkError("连接中断"), "发送结果未知"),
                (ValueError("无效参数"), "发送被拒绝"),
            ):
                with self.subTest(command=command, result=result):
                    plugin = self._plugin()
                    client = mock.Mock(publish=mock.AsyncMock())
                    if isinstance(result, Exception):
                        client.publish.side_effect = result
                    else:
                        client.publish.return_value = result
                    plugin._get_client = lambda profile_id="": client

                    async def run():
                        event = AstrMessageEvent(role="admin", message="/xhh发帖 标题|正文|43")
                        if command:
                            return [reply async for reply in plugin.cmd_post(event)][0]
                        return await plugin.tool_publish(event, "标题", "正文", topic_ids="43")

                    self.assertIn(expected, await run())
                    client.publish.assert_awaited_once_with(
                        "标题", "正文", mode="dynamic", topic_ids=["43"], draft=False, view_limit=2,
                    )

    async def test_command_and_tool_share_sender_cooldown(self):
        for command_first in (True, False):
            with self.subTest(command_first=command_first):
                plugin = self._plugin()
                client = mock.Mock(publish=mock.AsyncMock(return_value={"link_id": "123"}))
                plugin._get_client = lambda profile_id="": client

                async def run():
                    event = AstrMessageEvent(role="admin", message="/xhh发帖 标题|正文|43")

                    async def command():
                        return [reply async for reply in plugin.cmd_post(event)][0]

                    async def tool():
                        return await plugin.tool_publish(event, "标题", "正文", topic_ids="43")

                    first, second = (command, tool) if command_first else (tool, command)
                    self.assertIn("发布成功", await first())
                    self.assertIn("太频繁", await second())

                await run()
                client.publish.assert_awaited_once()

    async def test_tool_preserves_publish_options_and_dry_run_review_payloads(self):
        for mode in ("dynamic", "article"):
            for draft in (False, True):
                for visibility in (1, 2, 3):
                    for dry_run in (False, True):
                        with self.subTest(mode=mode, draft=draft, visibility=visibility, dry_run=dry_run):
                            plugin = self._plugin(view_limit=visibility, dry_run=dry_run)
                            client = mock.Mock(publish=mock.AsyncMock(return_value={
                                "link_id": "123", "is_draft": draft,
                            }))
                            plugin._get_client = mock.Mock(return_value=client)
                            reply = await plugin.tool_publish(
                                AstrMessageEvent(role="admin"), "标题", "正文", topic_ids="43",
                                mode=mode, draft=draft,
                            )
                            review = plugin.publisher.queue.list()[0]
                            self.assertEqual(review["payload"], {
                                "title": "标题", "text": "正文", "topic_ids": ["43"],
                                "mode": mode, "draft": draft, "view_limit": visibility,
                            })
                            if dry_run:
                                plugin._get_client.assert_not_called()
                                self.assertIn("dry-run", reply)
                                self.assertEqual(review["status"], "pending")
                            else:
                                client.publish.assert_awaited_once_with(
                                    "标题", "正文", mode=mode, topic_ids=["43"], draft=draft,
                                    view_limit=visibility,
                                )
                                self.assertEqual(review["status"], "sent")
                                self.assertIn("草稿已保存" if draft else "发布成功", reply)

    async def test_initialization_respects_disabled_outbox_purge(self):
        for days in (0, 7):
            with self.subTest(days=days):
                plugin = self._plugin(outbox_purge_days=days)
                box = plugin._get_outbox()
                with mock.patch("time.time", return_value=time.time() - 30 * 86400):
                    box.mark_pending("old", {})
                    box.mark_failed("old")
                await plugin.initialize()
                self.assertEqual(box.purge(status="failed"), 1 if days <= 0 else 0)

    def test_error_logs_omit_server_payload(self):
        plugin = self._plugin()
        exc = XhhError("secret-message", status="failed", path="/bbs/app/api/link/post",
                       payload={"user_pkey": "secret-pkey", "msg": "secret-message"})
        with mock.patch("astrbot_plugin_heybox_poster.main.logger.warning") as warning:
            plugin._log_error(exc)
        self.assertNotIn("secret", warning.call_args.args[0])
        self.assertIn("/bbs/app/api/link/post", warning.call_args.args[0])

    def test_cooldown(self):
        plugin = self._plugin(cooldown_seconds=60)

        event = AstrMessageEvent("10001")
        self.assertEqual(plugin._check_cooldown(event), "")
        self.assertIn("太频繁", plugin._check_cooldown(event))

    def test_lock_per_sender(self):
        plugin = self._plugin()
        self.assertIs(plugin._lock_for("10001"), plugin._lock_for("10001"))
        self.assertIsNot(plugin._lock_for("10001"), plugin._lock_for("20002"))

    def test_friendly_error_categories(self):
        plugin = self._plugin()
        self.assertIn("风控", plugin._friendly_error(CaptchaRequired("触发验证码")))
        self.assertIn("稍后再试", plugin._friendly_error(RateLimited("发帖频率过快，请稍后再试")))
        self.assertIn("网络", plugin._friendly_error(NetworkError("连接超时")))
        self.assertIn("登录态", plugin._friendly_error(XhhError("登录失败", status="login")))
        self.assertIn("参数错误", plugin._friendly_error(ValueError("标题不能为空")))

    def test_default_outbox_path_falls_back(self):
        plugin = self._plugin()
        with mock.patch("astrbot.api.star.StarTools.get_data_dir", side_effect=RuntimeError("unavailable")):
            self.assertEqual(XhhPosterPlugin._default_outbox_path(plugin), ROOT / "outbox.sqlite3")

    async def test_service_rate_limit_survives_restart_and_is_account_scoped(self):
        plugin = self._plugin(cooldown_seconds=0)
        client = mock.Mock(publish=mock.AsyncMock(side_effect=RateLimited("limited")))
        plugin._get_client = mock.Mock(return_value=client)
        reply = await plugin.tool_publish(
            AstrMessageEvent(role="admin"), "标题", "初次正文", topic_ids="43",
        )
        self.assertIn("发送被拒绝", reply)
        client.publish.assert_awaited_once()
        deadline = plugin._get_outbox().cooldown_until("id:1:publishing")
        self.assertGreater(deadline, time.time())
        path = plugin.runtime.data_dir / "outbox.sqlite3"
        config = FakeConfig(plugin.config)
        await plugin.terminate()

        with mock.patch.object(XhhPosterPlugin, "_default_outbox_path", return_value=path):
            reloaded = XhhPosterPlugin(None, config)
        reloaded._default_outbox_path = lambda: path
        self.addAsyncCleanup(reloaded.terminate)
        self.assertEqual(reloaded._get_outbox().cooldown_until("id:1:publishing"), deadline)
        new_client = mock.Mock(publish=mock.AsyncMock(return_value={"link_id": "123"}))
        reloaded._get_client = mock.Mock(return_value=new_client)
        event = AstrMessageEvent("another-sender", role="admin")
        self.assertIn("冷却", await reloaded.tool_publish(event, "标题", "重启正文", topic_ids="43"))
        reloaded._get_client.assert_not_called()

        self._set_credentials(reloaded, uid="2")
        self.assertIn("发布成功", await reloaded.tool_publish(event, "标题", "另一个账号", topic_ids="43"))
        new_client.publish.assert_awaited_once()
        self._set_credentials(reloaded, uid="1")
        self.assertIn("冷却", await reloaded.tool_publish(event, "标题", "切回原账号", topic_ids="43"))
        new_client.publish.assert_awaited_once()

    async def test_zero_service_rate_limit_allows_a_new_submission(self):
        plugin = self._plugin(cooldown_seconds=0, rate_limit_cooldown_seconds=0)
        client = mock.Mock(publish=mock.AsyncMock(side_effect=[
            RateLimited("limited"), {"link_id": "123"},
        ]))
        plugin._get_client = mock.Mock(return_value=client)
        event = AstrMessageEvent(role="admin")
        self.assertIn("发送被拒绝", await plugin.tool_publish(event, "标题", "第一次", topic_ids="43"))
        self.assertLessEqual(plugin._get_outbox().cooldown_until("id:1:publishing"), time.time())
        self.assertIn("发布成功", await plugin.tool_publish(event, "标题", "第二次", topic_ids="43"))
        self.assertEqual(client.publish.await_count, 2)

    async def test_publish_entrypoints_persist_account_rate_limit_and_block_retry(self):
        for command in (True, False):
            with self.subTest(command=command):
                plugin = self._plugin(cooldown_seconds=0)
                client = mock.Mock(publish=mock.AsyncMock(side_effect=RateLimited("secret payload")))
                plugin._get_client = lambda profile_id="": client
                event = AstrMessageEvent("10001", "/xhh发帖 标题|正文|43", role="admin")

                async def run():
                    if command:
                        return [reply async for reply in plugin.cmd_post(event)][0]
                    return await plugin.tool_publish(event, "标题", "正文", topic_ids="43")

                first = await run()
                self.assertIn("发送被拒绝", first)
                self.assertNotIn("secret", first)
                self.assertGreater(plugin._get_outbox().cooldown_until("id:1:publishing"), time.time())
                self.assertIn("冷却", await run())
                client.publish.assert_awaited_once()

    async def test_resolve_topics_by_name(self):
        plugin = self._plugin()
        plugin._get_client = lambda profile_id="": FakeClient()
        ids = await plugin._resolve_topics("Steam")
        self.assertEqual(ids, ["43"])

    async def test_resolve_topics_priority(self):
        plugin = self._plugin(default_topic_ids=" 99， 100, , ")
        plugin._get_client = lambda profile_id="": FakeClient()
        self.assertEqual(await plugin._resolve_topics("Steam", " 7，8, , "), ["7", "8"])
        self.assertEqual(await plugin._resolve_topics("Steam"), ["43"])
        self.assertEqual(await plugin._resolve_topics(" 123 "), ["123"])
        self.assertEqual(await plugin._resolve_topics("", ""), ["99", "100"])

    async def test_missing_topic_raises(self):
        plugin = self._plugin()
        plugin._get_client = lambda profile_id="": FakeClient()
        with self.assertRaises(Exception):
            await plugin._resolve_topics("不存在的话题")

    async def test_publish_keeps_resolved_profile_when_default_changes_during_topic_search(self):
        plugin = self._plugin(post_review_required=True, cooldown_seconds=0)
        secondary = dict(plugin.config["profiles"][0], profile_id="secondary")
        plugin.config["profiles"].append(secondary)
        self._set_credentials(plugin, uid="2", profile_id="secondary")

        started = asyncio.Event()
        release = asyncio.Event()

        class GatedClient:
            async def search_topics(self, keyword, limit=5):
                started.set()
                await release.wait()
                return [{"topic_id": "43", "name": keyword}]

        plugin._get_client = mock.Mock(return_value=GatedClient())
        task = asyncio.create_task(
            plugin.tool_publish(
                AstrMessageEvent(role="admin"), "标题", "正文", topic="Steam"
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        plugin.config["publishing"]["profile_id"] = "secondary"
        release.set()

        reply = await asyncio.wait_for(task, timeout=2)
        self.assertIn("已提交审核", reply)
        review = plugin.publisher.queue.list()[0]
        self.assertEqual(review["profile_id"], "default")
        self.assertEqual(review["account_id"], "1")

    async def test_publish_rejects_uid_change_during_topic_search(self):
        plugin = self._plugin(post_review_required=True, cooldown_seconds=0)
        started = asyncio.Event()
        release = asyncio.Event()

        class GatedClient:
            async def search_topics(self, keyword, limit=5):
                started.set()
                await release.wait()
                return [{"topic_id": "43", "name": keyword}]

        plugin._get_client = mock.Mock(return_value=GatedClient())
        task = asyncio.create_task(
            plugin.tool_publish(
                AstrMessageEvent(role="admin"), "标题", "正文", topic="Steam"
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        self._set_credentials(plugin, uid="2")
        release.set()

        reply = await asyncio.wait_for(task, timeout=2)
        self.assertIn("账号已切换", reply)
        self.assertEqual(plugin.publisher.queue.list(), [])


    async def test_post_command_and_tool_submit_one_review_without_sending(self):
        plugin = self._plugin(post_review_required=True, cooldown_seconds=0)
        plugin._get_client = mock.Mock(side_effect=AssertionError("must wait for review"))
        event = AstrMessageEvent(role="admin", message="/xhh发帖 标题|正文|43")

        command = [reply async for reply in plugin.cmd_post(event)][0]
        tool = await plugin.tool_publish(event, "标题", "正文", topic_ids="43")

        self.assertIn("已提交审核", command)
        self.assertEqual(command, tool)
        reviews = plugin.publisher.queue.list()
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["status"], "pending")
        self.assertEqual(reviews[0]["account_id"], "1")
        self.assertEqual(reviews[0]["source"], "10001")
        plugin._get_client.assert_not_called()

    async def test_comment_command_and_tool_respect_permission_and_review(self):
        plugin = self._plugin(comment_review_required=True)
        plugin.runtime.get_client = mock.AsyncMock(side_effect=AssertionError("must wait for review"))
        outsider = AstrMessageEvent(message="/xhh评论 123|评论|456")
        self.assertIn("权限", await plugin.tool_comment(outsider, "123", "评论"))
        denied = [reply async for reply in plugin.cmd_comment(outsider)][0]
        self.assertIn("权限", denied)
        self.assertEqual(plugin.publisher.queue.list(), [])

        event = AstrMessageEvent(role="admin", message="/xhh评论 123|评论|456")
        command = [reply async for reply in plugin.cmd_comment(event)][0]
        tool = await plugin.tool_comment(event, "123", "评论", parent_comment_id="456")
        self.assertIn("已提交审核", command)
        self.assertEqual(command, tool)
        review = plugin.publisher.queue.list()[0]
        self.assertEqual(review["kind"], "comment")
        self.assertEqual(review["payload"]["parent_comment_id"], "456")
        plugin.runtime.get_client.assert_not_awaited()

    async def test_profile_dry_run_cannot_be_bypassed_by_publish_settings(self):
        plugin = self._plugin(dry_run=False)
        plugin.config["profiles"][0]["dry_run"] = True
        plugin._get_client = mock.Mock(side_effect=AssertionError("profile is preview only"))
        reply = await plugin.tool_publish(AstrMessageEvent(role="admin"), "标题", "正文", topic_ids="43")
        self.assertIn("dry-run", reply)
        self.assertEqual(plugin.publisher.queue.list()[0]["status"], "pending")
        plugin._get_client.assert_not_called()

    async def test_dry_run_with_explicit_topics_does_not_require_login(self):
        plugin = self._plugin(dry_run=True)
        plugin.runtime.credentials.delete("default")
        plugin._get_client = mock.Mock(side_effect=AssertionError("preview must stay offline"))
        reply = await plugin.tool_publish(AstrMessageEvent(role="admin"), "标题", "正文", topic_ids="43")
        self.assertIn("dry-run", reply)
        plugin._get_client.assert_not_called()

    async def test_main_forwards_all_native_hooks_with_original_priorities(self):
        plugin = self._plugin()
        event, request, tool, run_context, response = (object() for _ in range(5))
        cases = (
            ("prepare_xiaoheihe_before_agent", "on_waiting_llm_request", 1000, (event,)),
            ("inject_xiaoheihe_context", "on_llm_request", None, (event, request)),
            ("isolate_xiaoheihe_images_for_grok", "on_using_llm_tool", -1000, (event, tool, {})),
            ("restore_xiaoheihe_images_after_grok", "on_llm_tool_respond", 1000, (event, tool, {}, None)),
            ("restore_xiaoheihe_images_on_agent_done", "on_agent_done", 1000, (event, run_context, response)),
            ("capture_xiaoheihe_complete_reply", "on_llm_response", -1000, (event, response)),
            ("mark_xiaoheihe_agent_started", "on_agent_begin", 1000, (event, run_context)),
            ("mark_xiaoheihe_agent_done", "on_agent_done", -1000, (event, run_context, response)),
        )
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        methods = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)}
        for name, hook, priority, arguments in cases:
            with self.subTest(method=name), mock.patch.object(plugin.community, name, new_callable=mock.AsyncMock) as callback:
                await getattr(plugin, name)(*arguments)
                callback.assert_awaited_once_with(*arguments)
                decorator = methods[name].decorator_list[0]
                self.assertEqual(decorator.func.attr, hook)
                priorities = [ast.literal_eval(item.value) for item in decorator.keywords if item.arg == "priority"]
                self.assertEqual(priorities, [] if priority is None else [priority])

    async def test_lifecycle_owns_one_shared_runtime(self):
        plugin = self._plugin()
        self.assertIs(plugin.runtime, plugin.community.runtime)
        self.assertIs(plugin.runtime.publisher, plugin.publisher)
        await plugin.initialize()
        self.assertTrue(plugin.runtime._started)
        await plugin.terminate()
        self.assertTrue(plugin.runtime._closed)
        self.assertFalse(plugin.runtime._started)


if __name__ == "__main__":
    unittest.main()
