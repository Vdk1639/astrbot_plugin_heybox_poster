"""Shared AstrBot stubs and real, isolated runtime fixtures for integration tests."""

from __future__ import annotations

import copy
import logging
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT.parent):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from adapter_tests.astrbot_stubs import install  # noqa: E402

install()

from astrbot.api import event as event_api  # noqa: E402
from astrbot.api.event import AstrMessageEvent  # noqa: E402
from astrbot.api.platform import AstrBotMessage, MessageMember, PlatformMetadata  # noqa: E402
import astrbot.api as astrbot_api  # noqa: E402

astrbot_api.logger = logging.getLogger("test-astrbot")
event_api.filter.command = lambda *args, **kwargs: lambda fn: fn
event_api.filter.llm_tool = lambda *args, **kwargs: lambda fn: fn

from astrbot_plugin_heybox_poster.main import XhhPosterPlugin  # noqa: E402
from astrbot_plugin_heybox_poster.xiaoheihe.config_service import DEFAULT_CONFIG  # noqa: E402
from astrbot_plugin_heybox_poster.xiaoheihe.models import Credentials  # noqa: E402


class FakeConfig(dict):
    def __init__(self, value=None, **kwargs):
        super().__init__(copy.deepcopy(value or kwargs))
        self.save_count = 0
        self.saved = None

    def save_config(self):
        self.save_count += 1
        self.saved = copy.deepcopy(dict(self))


class Context:
    def __init__(self, config=None):
        self._config = config or {}

    def get_config(self, **kwargs):
        return self._config


class SmokeEvent(AstrMessageEvent):
    def __init__(self, sender="10001", message="", role=None, platform=""):
        message_obj = AstrBotMessage()
        message_obj.sender = MessageMember(sender, "test user")
        message_obj.message = []
        super().__init__(message, message_obj, PlatformMetadata(platform, "test", "test"), "test")
        self.role = role
        self._platform = platform

    def get_platform_name(self):
        if not self._platform:
            raise AttributeError("platform unavailable")
        return self._platform

    def is_admin(self):
        return self.role == "admin"

    def plain_result(self, text):
        return text


class PluginTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target in (
            "urllib.request.urlopen",
            "urllib.request.OpenerDirector.open",
            "httpx.AsyncClient.send",
        ):
            guard = self.enterContext(mock.patch(target, side_effect=AssertionError("network forbidden")))
            self.addCleanup(guard.assert_not_called)

    def _plugin(self, context=None, *, raw_config=None, **settings):
        directory = pathlib.Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="heybox-test-"))
        )
        path = directory / "outbox.sqlite3"
        if raw_config is None:
            profile = copy.deepcopy(DEFAULT_CONFIG["profiles"][0])
            profile["dry_run"] = False
            raw_config = FakeConfig({
                "publishing": {
                    "dry_run": False,
                    "post_review_required": False,
                    "comment_review_required": False,
                    **settings,
                },
                "profiles": [profile],
            })
        with mock.patch.object(XhhPosterPlugin, "_default_outbox_path", return_value=path):
            plugin = XhhPosterPlugin(context, raw_config)
        plugin._default_outbox_path = lambda: path
        self.addAsyncCleanup(plugin.terminate)
        if plugin.runtime.credentials.load("default") is None:
            self._set_credentials(plugin)
        return plugin

    @staticmethod
    def _set_credentials(plugin, uid="1", cookie="a", profile_id="default"):
        plugin.runtime.credentials.save(Credentials(
            profile_id=profile_id, uid=uid, nickname="测试号",
            cookies={"user_pkey": cookie, "x_xhh_tokenid": "b"},
        ))
