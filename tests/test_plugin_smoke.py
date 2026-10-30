"""不装 AstrBot 也能跑的插件冒烟测试：用桩模块替代 ``astrbot``。

只验证插件自身的装配逻辑（配置解析、白名单、话题解析、频控），
不涉及 AstrBot 真实运行时与真实网络。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _install_astrbot_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class AstrBotConfig(dict):
        pass

    import logging

    api.AstrBotConfig = AstrBotConfig
    api.logger = logging.getLogger("test-astrbot")

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        def __init__(self, sender: str = "10001", message: str = "", role: str | None = None,
                     platform: str = ""):
            self._sender = sender
            self.message_str = message
            self.role = role
            self._platform = platform

        def get_sender_id(self):
            return self._sender

        def is_admin(self):
            return self.role == "admin"

        def get_platform_name(self):
            if not self._platform:
                raise AttributeError("platform unavailable")
            return self._platform

        def plain_result(self, text):
            return text

    class _Filter:
        @staticmethod
        def command(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def llm_tool(*args, **kwargs):
            return lambda fn: fn

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter

    star_mod = types.ModuleType("astrbot.api.star")

    class Context:
        def __init__(self, config=None):
            self._config = config if config is not None else {}

        def get_config(self):
            return self._config

    class Star:
        def __init__(self, context=None):
            self.context = context

    def register(*args, **kwargs):
        return lambda cls: cls

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod


_install_astrbot_stubs()
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_heybox_poster.main import XhhPosterPlugin  # noqa: E402
from astrbot_plugin_heybox_poster.core.client import (  # noqa: E402
    CaptchaRequired,
    NetworkError,
    RateLimited,
    XhhError,
)
from astrbot_plugin_heybox_poster.core.outbox import Outbox  # noqa: E402


class FakeClient:
    async def search_topics(self, keyword, limit=5):
        return [{"topic_id": "43", "name": "Steam"}] if keyword == "Steam" else []

    async def account_status(self):
        return {"logged_in": True, "nickname": "测试号", "user_id": "1"}


class PluginSmokeTests(unittest.TestCase):
    def _plugin(self, context=None, **config):
        base = {"cookie": "user_pkey=a; x_xhh_tokenid=b", "heybox_id": "1"}
        base.update(config)
        plugin = XhhPosterPlugin(context=context, config=base)
        box = Outbox(":memory:")
        self.addCleanup(box.close)
        plugin._get_outbox = lambda: box
        return plugin

    def test_cookie_composition(self):
        plugin = self._plugin(cookie="", user_pkey="p", x_xhh_tokenid="t")
        self.assertEqual(plugin._cookie(), "user_pkey=p; x_xhh_tokenid=t")

    def test_account_config_change_rebuilds_client(self):
        plugin = self._plugin()
        plugin._get_outbox = lambda: None

        async def run():
            first = plugin._get_client()
            plugin.config["heybox_id"] = "2"
            second = plugin._get_client()
            self.assertIsNot(first, second)
            self.assertNotEqual(first._account_key, second._account_key)
            await plugin.terminate()

        asyncio.run(run())

    def test_whitelist(self):
        plugin = self._plugin(allowed_users=["10001"])
        from astrbot.api.event import AstrMessageEvent

        self.assertTrue(plugin._is_allowed(AstrMessageEvent("10001")))
        self.assertFalse(plugin._is_allowed(AstrMessageEvent("20002")))

    def test_platform_prefixes_isolate_same_sender_ids(self):
        from astrbot.api.event import AstrMessageEvent

        plugin = self._plugin(allowed_users=["qq:123", "xhh:456"])
        for sender, platform, expected in (
            ("123", "aiocqhttp", True), ("123", "qq_official", True),
            ("123", "xiaoheihe", False), ("456", "heybox", True),
            ("456", "aiocqhttp", False), ("123", "discord", False),
        ):
            with self.subTest(sender=sender, platform=platform):
                event = AstrMessageEvent(sender, platform=platform)
                self.assertEqual(plugin._is_allowed(event), expected)

    def test_bare_whitelist_ids_keep_current_platform_compatibility(self):
        from astrbot.api.event import AstrMessageEvent

        plugin = self._plugin(allowed_users=["123"])
        for platform in ("aiocqhttp", "xiaoheihe"):
            self.assertTrue(plugin._is_allowed(AstrMessageEvent("123", platform=platform)))

    def test_platform_cooldowns_and_locks_are_separate(self):
        from astrbot.api.event import AstrMessageEvent

        plugin = self._plugin()
        qq = AstrMessageEvent("123", platform="aiocqhttp")
        xhh = AstrMessageEvent("123", platform="xiaoheihe")
        self.assertEqual(plugin._check_cooldown(qq), "")
        self.assertEqual(plugin._check_cooldown(xhh), "")
        self.assertIsNot(plugin._lock_for(plugin._sender(qq)), plugin._lock_for(plugin._sender(xhh)))

    def test_empty_whitelist_admin_role_allowed(self):
        plugin = self._plugin()
        from astrbot.api.event import AstrMessageEvent

        self.assertTrue(plugin._is_allowed(AstrMessageEvent("10001", role="admin")))

    def test_empty_whitelist_non_admin_denied_when_admins_configured(self):
        from astrbot.api.event import AstrMessageEvent
        from astrbot.api.star import Context

        plugin = self._plugin(context=Context({"admins_id": ["10001"]}))
        self.assertTrue(plugin._is_allowed(AstrMessageEvent("10001")))
        self.assertFalse(plugin._is_allowed(AstrMessageEvent("20002")))

    def test_empty_whitelist_denies_when_no_admin_source(self):
        plugin = self._plugin()
        from astrbot.api.event import AstrMessageEvent

        self.assertFalse(plugin._is_allowed(AstrMessageEvent("anyone")))

    def test_admin_lookup_failure_denies_access(self):
        from astrbot.api.event import AstrMessageEvent

        plugin = self._plugin(context=mock.Mock())
        plugin.context.get_config.side_effect = RuntimeError("unavailable")
        event = AstrMessageEvent("anyone")
        event.is_admin = mock.Mock(side_effect=RuntimeError("unavailable"))
        self.assertFalse(plugin._is_allowed(event))

    def test_delete_obeys_dry_run(self):
        from astrbot.api.event import AstrMessageEvent

        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                plugin = self._plugin(allowed_users=["10001"], dry_run=dry_run)
                client = mock.Mock(delete_link=mock.AsyncMock(return_value={}))
                plugin._get_client = mock.Mock(return_value=client)
                event = AstrMessageEvent("10001", "/xhh删除 123")
                event.plain_result = str

                async def run():
                    return [reply async for reply in plugin.cmd_delete(event)]

                replies = asyncio.run(run())
                if dry_run:
                    plugin._get_client.assert_not_called()
                    self.assertIn("dry-run", replies[0])
                else:
                    client.delete_link.assert_awaited_once_with("123")

    def test_missing_admin_denies_write_entrypoints(self):
        from astrbot.api.event import AstrMessageEvent

        plugin = self._plugin()
        plugin._get_client = mock.Mock(side_effect=AssertionError("must not send"))
        event = AstrMessageEvent("outsider", "/xhh删除 123")
        event.plain_result = str

        async def run():
            self.assertIn("权限", await plugin.tool_publish(event, "标题", "正文", topic_ids="43"))
            for command in (plugin.cmd_post, plugin.cmd_delete):
                replies = [reply async for reply in command(event)]
                self.assertIn("权限", replies[0])

        asyncio.run(run())
        plugin._get_client.assert_not_called()

    def test_cooldown(self):
        plugin = self._plugin(cooldown_seconds=60)
        from astrbot.api.event import AstrMessageEvent

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
        self.assertEqual(plugin._default_outbox_path().name, "outbox.sqlite3")

    def test_rate_limit_block(self):
        plugin = self._plugin()
        plugin._record_rate_limit("10001")
        self.assertIn("秒后再试", plugin._rate_limit_block("10001"))
        self.assertEqual(plugin._rate_limit_block("20002"), "")

    def test_rate_limit_survives_plugin_recreation_and_is_account_scoped(self):
        plugin = self._plugin()
        plugin._record_rate_limit("qq:123")
        reloaded = self._plugin()
        reloaded._get_outbox = plugin._get_outbox
        self.assertIn("秒后再试", reloaded._rate_limit_block("qq:123"))
        self.assertEqual(reloaded._rate_limit_block("xhh:123"), "")
        reloaded.config["heybox_id"] = "another"
        self.assertEqual(reloaded._rate_limit_block("qq:123"), "")

    def test_zero_rate_limit_cooldown_disables_persistence(self):
        plugin = self._plugin(rate_limit_cooldown_seconds=0)
        plugin._record_rate_limit("qq:123")
        self.assertEqual(plugin._rate_limit_seconds(), 0)
        self.assertEqual(plugin._rate_limit_block("qq:123"), "")
        self.assertEqual(plugin._get_outbox().cooldown_until(plugin._rate_limit_key("qq:123")), 0)

    def test_tool_publish_rate_limited_sets_cooldown(self):
        from astrbot_plugin_heybox_poster.core.client import RateLimited
        from astrbot.api.event import AstrMessageEvent

        plugin = self._plugin()
        event = AstrMessageEvent("10001", role="admin")

        class FailingClient:
            async def publish(self, *args, **kwargs):
                raise RateLimited("发帖频率过快，请稍后再试")

        plugin._get_client = lambda: FailingClient()
        result = asyncio.run(
            plugin.tool_publish(event, "标题", "正文", topic_ids="43")
        )
        self.assertIn("稍后再试", result)
        self.assertGreater(plugin._get_outbox().cooldown_until(plugin._rate_limit_key("10001")), time.time())

    def test_resolve_topics_by_name(self):
        plugin = self._plugin()
        plugin._get_client = lambda: FakeClient()
        ids = asyncio.run(plugin._resolve_topics("Steam"))
        self.assertEqual(ids, ["43"])

    def test_resolve_topics_priority(self):
        plugin = self._plugin(default_topic_ids="99")
        plugin._get_client = lambda: FakeClient()
        self.assertEqual(asyncio.run(plugin._resolve_topics("", "7,8")), ["7", "8"])
        self.assertEqual(asyncio.run(plugin._resolve_topics("", "")), ["99"])

    def test_missing_topic_raises(self):
        plugin = self._plugin()
        plugin._get_client = lambda: FakeClient()
        with self.assertRaises(Exception):
            asyncio.run(plugin._resolve_topics("不存在的话题"))


if __name__ == "__main__":
    unittest.main()
