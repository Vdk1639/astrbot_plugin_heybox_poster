"""Unified Dashboard integration; credential persistence belongs to auth-service tests.

The retired AccountWebUI polling/save-login checks are replaced by public auth
route, confirmation, review, schema and redaction contracts using one real runtime.
"""

from __future__ import annotations

import copy
import json
import types
import unittest
from unittest import mock

from plugin_test_support import Context, PluginTestCase
from adapter_tests.astrbot_stubs import Query
from astrbot_plugin_heybox_poster.core import dashboard as dashboard_module
from astrbot_plugin_heybox_poster.core.dashboard import Dashboard
from astrbot_plugin_heybox_poster.xiaoheihe import web_api as web_module
from astrbot_plugin_heybox_poster.xiaoheihe.runtime import PLUGIN_NAME


class DashboardTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.context = Context()
        self.context.register_web_api = mock.Mock()
        provider = types.SimpleNamespace(
            provider_config={"id": "provider-a"}, get_model=lambda: "test-model",
        )
        self.context.get_all_providers = lambda: [provider]
        self.plugin = self._plugin(
            context=self.context, post_review_required=True,
            comment_review_required=True, cooldown_seconds=0,
        )
        self.runtime = self.plugin.runtime
        self.web = self.plugin.web
        self.request = types.SimpleNamespace(
            username="admin", query=Query({}), json=mock.AsyncMock(return_value={}),
        )
        # Both controllers share this per-test request; never replace sys.modules.
        self.enterContext(mock.patch.object(dashboard_module, "request", self.request))
        self.enterContext(mock.patch.object(web_module, "request", self.request))

    async def _submit(self, kind="post"):
        payload = ({"title": "标题", "text": "正文", "topic_ids": ["43"]}
                   if kind == "post" else {"post_id": "123", "text": "评论"})
        self.request.json.return_value = {
            "kind": kind, "profile_id": "default", "payload": payload,
        }
        response = await self.web.submit()
        self.assertEqual(response["status_code"], 200)
        self.assertEqual(response["json"]["status"], "pending")
        return response["json"]["review"]["id"]

    async def test_all_registered_routes_deny_unauthorized_before_side_effects(self):
        self.request.username = None
        config = self.runtime.config.snapshot()
        credentials = self.runtime.credentials.load("default")
        with mock.patch.object(self.runtime, "ensure_started", new_callable=mock.AsyncMock) as start:
            for call in self.context.register_web_api.call_args_list:
                route, handler, _, _ = call.args
                with self.subTest(route=route):
                    args = ("1",) if "<" in route else ()
                    response = await handler(*args)
                    self.assertEqual(response["status_code"], 401)
            start.assert_not_awaited()
        self.request.json.assert_not_awaited()
        self.assertEqual(self.runtime.config.snapshot(), config)
        self.assertEqual(self.runtime.credentials.load("default"), credentials)
        self.assertEqual(self.plugin.publisher.queue.list(), [])
        self.assertFalse(self.runtime._started)

    def test_integrated_plugin_registers_one_dashboard_with_expected_routes(self):
        self.assertIsInstance(self.web, Dashboard)
        self.assertIs(self.web.runtime, self.plugin.runtime)
        calls = self.context.register_web_api.call_args_list
        routes = {call.args[0]: call.args[2] for call in calls}
        self.assertEqual(len(routes), len(calls))
        self.assertTrue(all(route.startswith(f"/{PLUGIN_NAME}/") for route in routes))
        for suffix in ("status", "auth/status", "config/schema", "publishing/status", "publishing/reviews"):
            self.assertEqual(routes[f"/{PLUGIN_NAME}/{suffix}"], ["GET"])
        for suffix in ("auth/qr", "auth/check", "auth/logout", "publishing/submit",
                       "publishing/reviews/<review_id>/approve", "publishing/reviews/<review_id>/reject"):
            self.assertEqual(routes[f"/{PLUGIN_NAME}/{suffix}"], ["POST"])
        self.assertNotIn(f"/{PLUGIN_NAME}/auth/cancel", routes)

    async def test_logout_and_approval_require_literal_confirmation(self):
        review_id = await self._submit()
        for body in ({}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1}, [], None):
            with self.subTest(body=body), \
                 mock.patch.object(self.runtime.auth, "logout", new_callable=mock.AsyncMock) as logout, \
                 mock.patch.object(self.plugin.publisher, "approve", new_callable=mock.AsyncMock) as approve:
                self.request.json.return_value = body
                self.assertEqual((await self.web.auth_logout())["status_code"], 400)
                self.assertEqual((await self.web.approve(str(review_id)))["status_code"], 400)
                self.assertEqual((await self.web.feed_approve("7"))["status_code"], 400)
                logout.assert_not_awaited()
                approve.assert_not_awaited()
                self.assertTrue(self.runtime.credentials.exists("default"))
                self.assertEqual(self.plugin.publisher.queue.get(review_id)["status"], "pending")
        self.assertFalse(self.runtime._started)

    async def test_confirmed_logout_updates_canonical_status_and_selected_profile_only(self):
        self._set_credentials(self.plugin, uid="2", profile_id="secondary")
        self.request.json.return_value = {"profile_id": "default", "confirm": True}
        with mock.patch.object(self.runtime, "notify_profile_changed", new_callable=mock.AsyncMock) as notify:
            response = await self.web.auth_logout()
        self.assertEqual(response["status_code"], 200)
        self.assertFalse(response["json"]["has_credentials"])
        self.assertFalse(self.runtime.credentials.exists("default"))
        self.assertEqual(self.runtime.credentials.load("secondary").uid, "2")
        notify.assert_awaited_once_with("default")
        status = await self.web.auth_status()
        self.assertFalse(status["json"]["has_credentials"])
        self.assertEqual(status["json"]["state"], "logged_out")

    async def test_auth_routes_delegate_to_unified_service(self):
        self.request.json.return_value = {"profile_id": "default"}
        with mock.patch.object(self.runtime.auth, "request_qr", new=mock.AsyncMock(return_value={"state": "waiting"})) as qr, \
             mock.patch.object(self.runtime, "notify_profile_changed", new_callable=mock.AsyncMock) as notify:
            response = await self.web.auth_qr()
            self.assertEqual(response["status_code"], 200)
            qr.assert_awaited_once_with("default")
            notify.assert_awaited_once_with("default", preserve_anonymous=True)
        with mock.patch.object(self.runtime.auth, "check", new=mock.AsyncMock(return_value={"state": "success"})) as check, \
             mock.patch.object(self.runtime, "notify_profile_changed", new_callable=mock.AsyncMock) as notify:
            response = await self.web.auth_check()
            self.assertEqual(response["json"]["state"], "success")
            check.assert_awaited_once_with("default")
            notify.assert_awaited_once_with("default")

    async def test_auth_rejects_nonobject_body_and_unsafe_profile_without_worker_calls(self):
        for method, service in ((self.web.auth_qr, "request_qr"), (self.web.auth_check, "check"), (self.web.auth_logout, "logout")):
            for body in ([], None, {"profile_id": "../outside", "confirm": True}):
                with self.subTest(method=service, body=body), \
                     mock.patch.object(self.runtime.auth, service, new_callable=mock.AsyncMock) as worker:
                    self.request.json.return_value = body
                    self.assertEqual((await method())["status_code"], 400)
                    worker.assert_not_awaited()
        self.assertFalse(self.runtime._started)

    async def test_auth_errors_redact_credential_details(self):
        self.request.json.return_value = {"profile_id": "default"}
        for method, service in ((self.web.auth_qr, "request_qr"), (self.web.auth_check, "check")):
            with self.subTest(service=service), mock.patch.object(
                self.runtime.auth, service, new=mock.AsyncMock(side_effect=RuntimeError("token=private-token"))
            ):
                response = await method()
                self.assertEqual(response["status_code"], 502)
                self.assertNotIn("private-token", json.dumps(response))
        self.assertTrue(self.runtime.credentials.exists("default"))

    async def test_status_and_configuration_never_expose_migration_credentials(self):
        self.runtime.config.raw.update(cookie="user_pkey=private-cookie", user_pkey="private-pkey")
        for method in (self.web.status, self.web.auth_status, self.web.publishing_status,
                       self.web.config_get, self.web.config_schema, self.web.diagnostics):
            with self.subTest(method=method.__name__):
                response = await method()
                self.assertEqual(response["status_code"], 200)
                encoded = json.dumps(response)
                for secret in ("private-cookie", "private-pkey", "user_pkey", "x_xhh_tokenid"):
                    self.assertNotIn(secret, encoded)
        self.assertTrue(self.runtime.credentials.exists("default"))

    async def test_provider_schema_uses_host_choices_and_filters_migration_fields(self):
        response = await self.web.config_schema()
        self.assertEqual(response["status_code"], 200)
        schema = response["json"]
        for name in ("llm_provider_id", "image_provider_id", "context_provider_id"):
            options = schema["providers"]["items"][name]["options"]
            self.assertEqual(options, [
                {"value": "", "label": "跟随当前配置"},
                {"value": "provider-a", "label": "provider-a · test-model"},
            ])
        self.assertTrue(schema["publishing"]["items"]["post_review_required"]["default"])
        self.assertTrue(schema["publishing"]["items"]["comment_review_required"]["default"])
        for key in ("cookie", "user_pkey", "x_xhh_tokenid", "heybox_id"):
            self.assertNotIn(key, schema)
        self.assertNotIn("topic_ids", schema["proactive_feed"]["items"])
        self.assertEqual(schema["profiles"]["type"], "template_list")

    async def test_config_save_uses_same_persisted_object_and_rejects_legacy_credentials(self):
        config = self.runtime.config.raw
        count = config.save_count
        replacement = copy.deepcopy((await self.web.config_get())["json"])
        replacement["reply"]["max_reply_chars"] = 321
        self.request.json.return_value = replacement
        self.assertEqual((await self.web.config_save())["status_code"], 200)
        self.assertIs(self.runtime.config.raw, config)
        self.assertEqual(config["reply"]["max_reply_chars"], 321)
        self.assertEqual(config.save_count, count + 1)
        self.request.json.return_value = {"cookie": "private-cookie"}
        self.assertEqual((await self.web.config_save())["status_code"], 400)
        self.assertEqual(config.save_count, count + 1)

    async def test_submit_lists_both_review_types_without_sending(self):
        with mock.patch.object(self.plugin, "_get_client") as post_client, \
             mock.patch.object(self.runtime, "get_client", new_callable=mock.AsyncMock) as comment_client:
            post_id = await self._submit("post")
            comment_id = await self._submit("comment")
            self.request.query = Query({"status": "pending"})
            response = await self.web.reviews()
            self.assertEqual(response["status_code"], 200)
            self.assertEqual({row["id"] for row in response["json"]["items"]}, {post_id, comment_id})
            self.request.query = Query({"kind": "comment", "limit": "1"})
            filtered = (await self.web.reviews())["json"]["items"]
            self.assertEqual([row["id"] for row in filtered], [comment_id])
            post_client.assert_not_called()
            comment_client.assert_not_awaited()

    async def test_confirmed_approval_and_async_rejection_update_review_status(self):
        approved_id = await self._submit("post")
        rejected_id = await self._submit("comment")
        client = mock.Mock(publish=mock.AsyncMock(return_value={"link_id": "123"}))
        self.plugin._get_client = mock.Mock(return_value=client)
        self.request.json.return_value = {"confirm": True}
        response = await self.web.approve(str(approved_id))
        self.assertEqual(response["status_code"], 200)
        self.assertEqual(response["json"]["status"], "sent")
        client.publish.assert_awaited_once()
        with mock.patch.object(self.plugin.publisher, "reject", wraps=self.plugin.publisher.reject) as reject:
            response = await self.web.reject(str(rejected_id))
            reject.assert_awaited_once_with(rejected_id)
        self.assertEqual(response["json"]["review"]["status"], "rejected")
        self.assertEqual((await self.web.approve(str(approved_id)))["status_code"], 400)
        client.publish.assert_awaited_once()

    async def test_approval_honors_dry_run_and_keeps_review_pending(self):
        review_id = await self._submit()
        self.runtime.config.raw["publishing"]["dry_run"] = True
        self.request.json.return_value = {"confirm": True}
        with mock.patch.object(self.plugin, "_get_client") as client:
            response = await self.web.approve(str(review_id))
        self.assertEqual(response["json"]["status"], "dry_run")
        self.assertEqual(self.plugin.publisher.queue.get(review_id)["status"], "pending")
        client.assert_not_called()

    async def test_account_change_blocks_approval_before_sending(self):
        review_id = await self._submit()
        self._set_credentials(self.plugin, uid="2")
        self.request.json.return_value = {"confirm": True}
        with mock.patch.object(self.plugin, "_get_client") as client:
            response = await self.web.approve(str(review_id))
        self.assertEqual(response["status_code"], 400)
        self.assertIn("账号已切换", response["json"]["message"])
        self.assertEqual(self.plugin.publisher.queue.get(review_id)["status"], "pending")
        client.assert_not_called()

    async def test_legacy_candidate_requires_a_second_account_bound_approval(self):
        candidate = {
            "id": 7, "status": "pending", "profile_id": "default",
            "post_id": "123", "generated_text": "旧候选正文", "incoming_event_id": None,
        }
        self.runtime.config.raw["publishing"]["comment_review_required"] = False
        self.runtime.config.raw["proactive_feed"]["dry_run"] = False
        self.request.json.return_value = {"confirm": True, "edited_text": "修改后的正文"}
        with mock.patch.object(self.runtime.repository, "feed_candidate", new=mock.AsyncMock(return_value=candidate)) as lookup, \
             mock.patch.object(self.runtime.repository, "review_feed_candidate", new_callable=mock.AsyncMock) as mark, \
             mock.patch.object(self.runtime, "get_client", new_callable=mock.AsyncMock) as client:
            response = await self.web.feed_approve("7")
            self.assertEqual(response["status_code"], 200)
            self.assertEqual(response["json"]["result"], "queued")
            review = response["json"]["review"]
            self.assertEqual(review["status"], "pending")
            self.assertEqual(review["kind"], "comment")
            self.assertEqual(review["source"], "legacy_candidate")
            self.assertEqual(review["source_id"], "candidate:7")
            self.assertEqual(review["account_id"], "1")
            self.assertEqual(review["payload"]["text"], "修改后的正文")
            lookup.assert_awaited_once_with(7)
            mark.assert_awaited_once_with(7, "approved", "修改后的正文")

            self._set_credentials(self.plugin, uid="2")
            self.request.json.return_value = {"confirm": True}
            rejected = await self.web.approve(str(review["id"]))
            self.assertEqual(rejected["status_code"], 400)
            self.assertIn("账号已切换", rejected["json"]["message"])
            self.assertEqual(self.plugin.publisher.queue.get(review["id"])["status"], "pending")
            client.assert_not_awaited()

    async def test_bad_submission_and_filters_return_400_without_queue_changes(self):
        for body in (None, [], {"kind": "unsupported", "payload": {}},
                     {"kind": "post", "profile_id": [], "payload": {}},
                     {"kind": "comment", "payload": {"post_id": "bad", "text": "评论"}}):
            with self.subTest(body=body):
                self.request.json.return_value = body
                self.assertEqual((await self.web.submit())["status_code"], 400)
        for query in ({"limit": "bad"}, {"kind": "unknown"}, {"status": "unknown"}):
            self.request.query = Query(query)
            self.assertEqual((await self.web.reviews())["status_code"], 400)
        self.assertEqual(self.plugin.publisher.queue.list(), [])

    async def test_unexpected_publish_errors_never_expose_raw_details(self):
        for method, arguments, service in (
            (self.web.submit, (), "submit"),
            (self.web.approve, ("1",), "approve"),
        ):
            self.request.json.return_value = {"confirm": True}
            with self.subTest(service=service), mock.patch.object(
                self.plugin.publisher, service,
                new=mock.AsyncMock(side_effect=RuntimeError("private-server-detail")),
            ):
                response = await method(*arguments)
                self.assertEqual(response["status_code"], 500)
                self.assertNotIn("private-server-detail", json.dumps(response))


if __name__ == "__main__":
    unittest.main()
