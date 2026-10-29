"""不装 AstrBot 也能跑的插件冒烟测试：用桩模块替代 ``astrbot``。

只验证插件自身的装配逻辑（配置解析、白名单、话题解析、频控），
不涉及 AstrBot 真实运行时与真实网络。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types
import unittest

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
        def __init__(self, sender: str = "10001", message: str = ""):
            self._sender = sender
            self.message_str = message

        def get_sender_id(self):
            return self._sender

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
        pass

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


class FakeClient:
    async def search_topics(self, keyword, limit=10):
        return [{"topic_id": "43", "name": "Steam"}] if keyword == "Steam" else []

    async def account_status(self):
        return {"logged_in": True, "nickname": "测试号", "user_id": "1"}


class PluginSmokeTests(unittest.TestCase):
    def _plugin(self, **config):
        base = {"cookie": "user_pkey=a; x_xhh_tokenid=b", "heybox_id": "1"}
        base.update(config)
        return XhhPosterPlugin(context=None, config=base)

    def test_cookie_composition(self):
        plugin = self._plugin(cookie="", user_pkey="p", x_xhh_tokenid="t")
        self.assertEqual(plugin._cookie(), "user_pkey=p; x_xhh_tokenid=t")

    def test_whitelist(self):
        plugin = self._plugin(allowed_users=["10001"])
        from astrbot.api.event import AstrMessageEvent

        self.assertTrue(plugin._is_allowed(AstrMessageEvent("10001")))
        self.assertFalse(plugin._is_allowed(AstrMessageEvent("20002")))

    def test_empty_whitelist_allows_all(self):
        plugin = self._plugin()
        from astrbot.api.event import AstrMessageEvent

        self.assertTrue(plugin._is_allowed(AstrMessageEvent("anyone")))

    def test_cooldown(self):
        plugin = self._plugin(cooldown_seconds=60)
        from astrbot.api.event import AstrMessageEvent

        event = AstrMessageEvent("10001")
        self.assertEqual(plugin._check_cooldown(event), "")
        self.assertIn("太频繁", plugin._check_cooldown(event))

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
