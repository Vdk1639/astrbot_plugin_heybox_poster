from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapter_tests.astrbot_stubs import REQUEST, Query
from adapter_tests.test_web_api import FakeContext, FakeRuntime
from adapter_tests.test_repository import make_notification
from xiaoheihe.models import EventState
from xiaoheihe.runtime import PLUGIN_NAME
from xiaoheihe.web_api import WebApiController


@pytest.fixture(autouse=True)
def clean_request(monkeypatch):
    monkeypatch.setattr(REQUEST, "username", "dashboard-admin")
    monkeypatch.setattr(REQUEST, "query", Query({}))
    monkeypatch.setattr(REQUEST, "_json", {})


@pytest.fixture
def api(fake_config):
    runtime = FakeRuntime(fake_config)
    runtime.repository = SimpleNamespace(
        has_profile_events=AsyncMock(return_value=False),
        list_events=AsyncMock(return_value={"items": [], "total": 0, "page": 1, "page_size": 30}),
        list_risk_users=AsyncMock(return_value={"items": [], "total": 0, "page": 1, "page_size": 30}),
        risk_user_detail=AsyncMock(return_value={
            "user": {"profile_id": "default", "sender_uid": "59051863", "has_record": False},
            "events": {"items": [], "total": 0, "page": 1, "page_size": 30},
        }),
        manage_risk_user=AsyncMock(return_value={"user": {"manual_blocked": True}}),
    )
    return WebApiController(runtime)


def mutation(**overrides):
    return {"profile_id": "default", "uid": "59051863", "confirm": True,
            "note": "管理员处置备注", **overrides}


def test_risk_routes_are_fixed_and_use_expected_methods(api):
    context = FakeContext()
    api.register(context)
    routes = {path: (handler, methods) for path, handler, methods, _ in context.routes}
    for suffix, name, method in (
        ("risk/users", "risk_users", "GET"),
        ("risk/users/detail", "risk_user_detail", "GET"),
        ("risk/users/block", "risk_user_block", "POST"),
        ("risk/users/unblock", "risk_user_unblock", "POST"),
        ("risk/users/adjust", "risk_user_adjust", "POST"),
    ):
        assert routes[f"/{PLUGIN_NAME}/{suffix}"] == (getattr(api, name), [method])


@pytest.mark.parametrize("handler", ["events", "risk_users", "risk_user_detail",
                                     "risk_user_block", "risk_user_unblock", "risk_user_adjust"])
async def test_routes_reuse_dashboard_auth_before_accessing_parameters(api, handler):
    REQUEST.username = None
    assert (await getattr(api, handler)())["status_code"] == 401
    for method in vars(api.runtime.repository).values():
        method.assert_not_awaited()


async def test_events_keep_flat_response_and_do_not_add_hidden_time_filter(api):
    result = {"items": [{"id": 7, "sender_uid": "1"}], "total": 1, "page": 1, "page_size": 30}
    api.runtime.repository.list_events.return_value = result
    response = await api.events()
    assert response == {"status_code": 200, "json": result}
    api.runtime.repository.list_events.assert_awaited_once_with(
        status="", uid="", post_id="", keyword="",
        start_time=None, end_time=None, page=1, page_size=30,
    )


async def test_events_forward_shared_filters_for_grouping_and_anonymous_drilldown(api):
    REQUEST.query = Query({
        "group_by": "uid", "profile_id": "default", "event_id": "8", "uid": "2",
        "status": "ignored", "post_id": "p1", "keyword": "content",
        "start_time": "1.5", "end_time": "2e1", "page": "0", "page_size": "101",
    })
    assert (await api.events())["status_code"] == 200
    api.runtime.repository.list_events.assert_awaited_once_with(
        group_by="uid", profile_id="default", event_id=8, status="ignored", uid="2", post_id="p1",
        keyword="content", start_time=1.5, end_time=20.0, page=1, page_size=100,
        risk_config={**api.runtime.config.snapshot()["risk"], "owner_uids": {"default": ""}},
    )


async def test_flat_events_work_with_existing_repository(fake_config, repository):
    runtime = FakeRuntime(fake_config)
    runtime.repository = repository
    response = await WebApiController(runtime).events()
    assert response == {"status_code": 200, "json": {"items": [], "total": 0, "page": 1, "page_size": 30}}


@pytest.mark.parametrize("query", [
    {"group_by": "nickname"}, {"group_by": "UID"}, {"status": "pending"},
    {"profile_id": "../default"}, {"profile_id": "unknown"}, {"profile_id": " default "},
    {"start_time": "nan"}, {"end_time": "inf"}, {"start_time": "-Infinity"},
    {"end_time": "1e999"}, {"start_time": "not-a-time"},
    {"start_time": "3", "end_time": "2"},
    *({"event_id": value} for value in ["0", "-1", "1.5", "+1", " 1", "1e2",
                                         "１", "local-anchor", "9223372036854775808"]),
])
async def test_invalid_event_parameters_do_not_query_events(api, query):
    REQUEST.query = Query(query)
    response = await api.events()
    assert response["status_code"] == 400
    assert response["json"]["message"] == "事件查询参数无效"
    api.runtime.repository.list_events.assert_not_awaited()


async def test_empty_times_and_pagination_remain_compatible(api):
    REQUEST.query = Query({"start_time": "", "end_time": "", "page": "invalid", "page_size": "-1"})
    assert (await api.events())["status_code"] == 200
    params = api.runtime.repository.list_events.await_args.kwargs
    assert params["start_time"] is params["end_time"] is None
    assert (params["page"], params["page_size"]) == (1, 1)


@pytest.mark.parametrize("handler", ["events", "risk_users", "risk_user_detail"])
async def test_historical_profile_is_readable(api, handler):
    api.runtime.repository.has_profile_events.return_value = True
    REQUEST.query = Query({"profile_id": "archived", "uid": "59051863"})
    assert (await getattr(api, handler)())["status_code"] == 200
    api.runtime.repository.has_profile_events.assert_awaited_once_with("archived")
    api.runtime.repository.manage_risk_user.assert_not_awaited()


async def test_risk_users_filter_types_and_empty_out_of_range_results(api):
    REQUEST.query = Query({"profile_id": "default", "uid": "59051863", "level": "watch",
                           "manual_blocked": "false", "page": "200", "page_size": "150"})
    response = await api.risk_users()
    assert response["status_code"] == 200
    assert response["json"]["items"] == []
    api.runtime.repository.list_risk_users.assert_awaited_once_with(
        profile_id="default", uid="59051863", level="watch", manual_blocked=False,
        page=200, page_size=100,
        risk_config={**api.runtime.config.snapshot()["risk"], "owner_uids": {"default": ""}},
    )


@pytest.mark.parametrize("query", [
    {"profile_id": "unknown"}, {"uid": "local-anchor"}, {"level": "critical"},
    {"manual_blocked": "1"}, {"manual_blocked": "False"},
])
async def test_invalid_risk_list_filters(api, query):
    REQUEST.query = Query(query)
    assert (await api.risk_users())["status_code"] == 400
    api.runtime.repository.list_risk_users.assert_not_awaited()


async def test_detail_returns_snapshot_and_paginated_audit_without_writes(api):
    REQUEST.query = Query({"profile_id": "default", "uid": "59051863", "page": "3", "page_size": "10"})
    response = await api.risk_user_detail()
    assert response == {"status_code": 200, "json": api.runtime.repository.risk_user_detail.return_value}
    assert response["json"]["user"]["has_record"] is False
    assert response["json"]["events"]["items"] == []
    api.runtime.repository.risk_user_detail.assert_awaited_once_with(
        "default", "59051863", page=3, page_size=10,
        risk_config={**api.runtime.config.snapshot()["risk"], "owner_uids": {"default": ""}},
    )
    api.runtime.repository.manage_risk_user.assert_not_awaited()


@pytest.mark.parametrize("query", [{}, {"uid": "1"}, {"profile_id": "default"},
                                       {"profile_id": "unknown", "uid": "1"}])
async def test_detail_requires_known_profile_and_uid(api, query):
    REQUEST.query = Query(query)
    assert (await api.risk_user_detail())["status_code"] == 400
    api.runtime.repository.risk_user_detail.assert_not_awaited()


@pytest.mark.parametrize("action", ["block", "unblock", "adjust"])
@pytest.mark.parametrize("overrides", [
    {"confirm": None}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1},
    {"profile_id": "archived"}, {"profile_id": "../default"}, {"profile_id": 1},
    {"uid": ""}, {"uid": "anchor_1"}, {"uid": True}, {"uid": 1.5}, {"uid": "1.0"},
    {"uid": "1e2"}, {"uid": "+1"}, {"uid": "-1"}, {"uid": "0"},
    {"uid": "１"}, {"uid": " 1"}, {"uid": "1" * 33},
    {"note": None}, {"note": []}, {"note": 1}, {"note": "字" * 201},
    {"delta": True}, {"delta": "1"}, {"delta": 1.5}, {"delta": 1001}, {"delta": -1001},
    {"reset_score": "false"}, {"reset_score": 1}, {"reset_score": None},
])
async def test_risk_mutations_validate_before_writing(api, action, overrides):
    api.runtime.repository.has_profile_events.return_value = True
    REQUEST._json = mutation(delta=10, **{key: value for key, value in overrides.items() if key != "delta"})
    if "delta" in overrides:
        REQUEST._json["delta"] = overrides["delta"]
    response = await getattr(api, f"risk_user_{action}")()
    assert response["status_code"] == 400
    api.runtime.repository.manage_risk_user.assert_not_awaited()


@pytest.mark.parametrize("action", ["block", "unblock", "adjust"])
@pytest.mark.parametrize("body", [[], "true", 1, None, {}])
async def test_risk_mutations_reject_non_objects_and_missing_confirmation(api, action, body):
    REQUEST._json = body
    assert (await getattr(api, f"risk_user_{action}")())["status_code"] == 400
    api.runtime.repository.manage_risk_user.assert_not_awaited()


async def test_adjust_requires_explicit_delta(api):
    REQUEST._json = mutation()
    assert (await api.risk_user_adjust())["status_code"] == 400
    api.runtime.repository.manage_risk_user.assert_not_awaited()


@pytest.mark.parametrize("note", ["", "字" * 200])
async def test_notes_allow_empty_text_and_the_documented_length_limit(api, note):
    REQUEST._json = mutation(note=note)
    assert (await api.risk_user_block())["status_code"] == 200
    assert api.runtime.repository.manage_risk_user.await_args.kwargs["note"] == note


async def test_grouping_passes_each_profiles_owner_without_persisting_runtime_metadata(api):
    profiles = api.runtime.config.raw["profiles"]
    profiles[0]["owner_uid"] = "123"
    profiles.append({**profiles[0], "profile_id": "secondary", "owner_uid": "456"})
    REQUEST.query = Query({"group_by": "uid"})
    assert (await api.events())["status_code"] == 200
    policy = api.runtime.repository.list_events.await_args.kwargs["risk_config"]
    assert policy["owner_uids"] == {"default": "123", "secondary": "456"}
    assert "owner_uids" not in api.runtime.config.raw["risk"]


async def test_unblock_rejects_non_boolean_reset_before_writing(api):
    REQUEST._json = mutation(reset_score="true")
    assert (await api.risk_user_unblock())["status_code"] == 400
    api.runtime.repository.manage_risk_user.assert_not_awaited()


@pytest.mark.parametrize("action,extra", [
    ("block", {}), ("unblock", {}), ("unblock", {"reset_score": True}),
    ("adjust", {"delta": -1000}), ("adjust", {"delta": 0}), ("adjust", {"delta": 1000}),
])
async def test_risk_mutations_use_authenticated_operator_and_redact_notes(api, action, extra):
    REQUEST._json = mutation(uid=59051863, note="token=private-value", operator="client-forged", **extra)
    response = await getattr(api, f"risk_user_{action}")()
    assert response["status_code"] == 200
    api.runtime.repository.manage_risk_user.assert_awaited_once_with(
        "default", "59051863", action=action, note="token=[REDACTED]", operator="dashboard-admin",
        delta=extra.get("delta", 0), reset_score=extra.get("reset_score", False),
        risk_config={**api.runtime.config.snapshot()["risk"], "owner_uids": {"default": ""}},
    )


@pytest.mark.parametrize("handler,method", [
    ("events", "list_events"), ("risk_users", "list_risk_users"),
    ("risk_user_detail", "risk_user_detail"), ("risk_user_block", "manage_risk_user"),
])
async def test_all_outputs_redact_nested_sensitive_data(api, handler, method):
    REQUEST.query = Query({"profile_id": "default", "uid": "59051863"})
    REQUEST._json = mutation()
    getattr(api.runtime.repository, method).return_value = {
        "items": [{"note": "token=private-value", "details": {"cookie": "raw-cookie"}}],
    }
    response = await getattr(api, handler)()
    assert response["status_code"] == 200
    assert "private-value" not in str(response)
    assert "raw-cookie" not in str(response)
    assert "[REDACTED]" in str(response)


@pytest.mark.parametrize("error", [ValueError, RuntimeError])
@pytest.mark.parametrize("handler,method", [
    ("events", "list_events"), ("risk_users", "list_risk_users"),
    ("risk_user_detail", "risk_user_detail"), ("risk_user_adjust", "manage_risk_user"),
])
async def test_repository_errors_use_fixed_messages_without_sql_or_exception_text(api, handler, method, error):
    REQUEST.query = Query({"profile_id": "default", "uid": "59051863"})
    REQUEST._json = mutation(delta=1)
    getattr(api.runtime.repository, method).side_effect = error("SELECT * FROM secret_table token=private-value")
    response = await getattr(api, handler)()
    assert response["status_code"] == 500
    assert "SELECT" not in str(response)
    assert "secret_table" not in str(response)
    assert "private-value" not in str(response)


@pytest.fixture
def real_api(fake_config, repository):
    runtime = FakeRuntime(fake_config)
    runtime.repository = repository
    return WebApiController(runtime)


async def test_repository_grouping_keeps_profiles_and_filtered_statistics_separate(real_api):
    repository = real_api.runtime.repository
    first = await repository.claim_event(make_notification("first", "59051863", "p1"))
    second = await repository.claim_event(make_notification("second", "59051863", "p2"))
    await repository.claim_event(replace(make_notification("history", "59051863"), profile_id="archived"))
    await repository.mark_event(first, EventState.SENT)
    await repository.mark_event(second, EventState.IGNORED)
    REQUEST.query = Query({"group_by": "uid"})
    response = await real_api.events()
    assert response["status_code"] == 200
    assert response["json"]["group_by"] == "uid"
    assert response["json"]["total"] == 2
    assert {row["profile_id"] for row in response["json"]["items"]} == {"default", "archived"}
    REQUEST.query = Query({"group_by": "uid", "profile_id": "default", "status": "ignored"})
    filtered = await real_api.events()
    assert filtered["status_code"] == 200
    assert filtered["json"]["total"] == 1
    user = filtered["json"]["items"][0]
    assert user["event_count"] == user["post_count"] == 1
    assert user["status_counts"] == {"ignored": 1}


async def test_repository_anonymous_groups_have_distinct_event_drilldowns(real_api):
    repository = real_api.runtime.repository
    first = await repository.claim_event(make_notification("anonymous-one", ""))
    second = await repository.claim_event(make_notification("anonymous-two", ""))
    REQUEST.query = Query({"group_by": "uid", "profile_id": "default"})
    response = await real_api.events()
    assert response["status_code"] == 200
    assert response["json"]["total"] == 2
    assert {row["event_id"] for row in response["json"]["items"]} == {first, second}
    assert all(row["identity_verified"] is False for row in response["json"]["items"])
    REQUEST.query = Query({"profile_id": "default", "event_id": str(first)})
    flat = await real_api.events()
    assert flat["status_code"] == 200
    assert [row["id"] for row in flat["json"]["items"]] == [first]


async def test_repository_risk_management_audits_and_unblock_resets(real_api):
    real_api.runtime.config.raw["risk"]["auto_filter_enabled"] = True
    REQUEST.query = Query({"profile_id": "default", "uid": "59051863", "page_size": "1"})
    REQUEST._json = mutation()
    assert (await real_api.risk_user_block())["status_code"] == 200
    repeated = await real_api.risk_user_block()
    assert repeated["status_code"] == 200
    assert repeated["json"]["changed"] is False
    REQUEST._json = mutation(delta=80)
    assert (await real_api.risk_user_adjust())["status_code"] == 200
    REQUEST._json = mutation()
    unblocked = await real_api.risk_user_unblock()
    assert unblocked["status_code"] == 200
    assert unblocked["json"]["user"]["manual_blocked"] is False
    assert unblocked["json"]["user"]["auto_filtered"] is True
    detail = await real_api.risk_user_detail()
    assert detail["status_code"] == 200
    assert detail["json"]["events"]["total"] == 3
    assert len(detail["json"]["events"]["items"]) == 1
    assert detail["json"]["events"]["items"][0]["operator"] == "dashboard-admin"
    REQUEST._json = mutation(reset_score=True)
    reset = await real_api.risk_user_unblock()
    assert reset["status_code"] == 200
    assert reset["json"]["user"]["score"] == 0
    assert reset["json"]["user"]["auto_filtered"] is False


async def test_repository_unrecorded_detail_and_historical_profile_are_read_only(real_api):
    repository = real_api.runtime.repository
    REQUEST.query = Query({"profile_id": "default", "uid": "59051863"})
    detail = await real_api.risk_user_detail()
    assert detail["status_code"] == 200
    assert detail["json"]["user"]["has_record"] is False
    assert detail["json"]["events"]["total"] == 0
    assert (await real_api.risk_users())["json"]["total"] == 0
    await repository.claim_event(replace(make_notification("archived", "59051863"), profile_id="archived"))
    REQUEST.query = Query({"profile_id": "archived", "uid": "59051863"})
    assert (await real_api.risk_user_detail())["status_code"] == 200
    REQUEST._json = mutation(profile_id="archived")
    assert (await real_api.risk_user_block())["status_code"] == 400
    REQUEST.query = Query({})
    assert (await real_api.risk_users())["json"]["total"] == 0


async def test_repository_owner_exemption_uses_profile_mapping_without_saving_it(real_api):
    real_api.runtime.config.raw["profiles"][0]["owner_uid"] = "59051863"
    real_api.runtime.config.raw["risk"]["auto_filter_enabled"] = True
    REQUEST._json = mutation(delta=80)
    response = await real_api.risk_user_adjust()
    assert response["status_code"] == 200
    assert response["json"]["user"]["auto_filtered"] is False
    REQUEST._json = mutation()
    blocked = await real_api.risk_user_block()
    assert blocked["status_code"] == 200
    assert blocked["json"]["user"]["manual_blocked"] is True
    REQUEST.query = Query({"profile_id": "default", "uid": "59051863"})
    assert (await real_api.risk_user_detail())["json"]["user"]["auto_filtered"] is False
    assert (await real_api.risk_users())["json"]["items"][0]["auto_filtered"] is False
    assert "owner_uids" not in real_api.runtime.config.snapshot()["risk"]
