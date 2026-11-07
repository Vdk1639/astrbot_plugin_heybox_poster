from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from xiaoheihe.config_service import (
    DEFAULT_CONFIG,
    ConfigService,
    ConfigValidationError,
)


def test_config_defaults_and_profile(fake_config) -> None:
    fake_config.pop("network")
    fake_config.pop("providers")
    service = ConfigService(fake_config)
    assert service.profile("default")["dry_run"] is True
    assert service.snapshot()["network"]["max_pending_events"] == 50
    assert service.snapshot()["providers"] == {
        "llm_provider_id": "",
        "image_provider_id": "",
        "context_provider_id": "",
    }
    assert service.snapshot()["context"]["thread_reply_post_chars"] == 1600
    assert service.snapshot()["context"]["thread_reply_recent_comments"] == 12
    assert service.snapshot()["context"]["enable_thread_reply_compression"] is True
    assert service.snapshot()["context"]["thread_reply_compression_trigger_chars"] == 2400
    assert service.snapshot()["context"]["thread_reply_compressed_post_chars"] == 700
    assert service.snapshot()["context"]["thread_reply_compressed_comments_chars"] == 1400
    assert service.snapshot()["context"]["thread_reply_compressed_image_chars"] == 800
    assert service.snapshot()["context"]["context_cache_ttl_seconds"] == 60
    assert service.snapshot()["context"]["image_total_timeout_seconds"] == 240
    assert service.snapshot()["reply"]["provider_fallback_grace_seconds"] == 60


@pytest.mark.asyncio
async def test_comment_corpus_defaults_survive_upgrade_save_and_schema(fake_config) -> None:
    fake_config.pop("comment_corpus", None)
    service = ConfigService(fake_config)
    expected = {
        "enabled": False,
        "min_level": 7,
        "collect_unknown_level": True,
        "excluded_uids": [],
        "max_per_day": 500,
        "max_rows": 20000,
    }
    assert service.snapshot()["comment_corpus"] == expected
    assert service.defaults()["comment_corpus"] == expected

    schema = service.ui_schema()
    raw_schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    for definition in (schema, raw_schema):
        assert definition["comment_corpus"]["items"]["enabled"]["default"] is False
        assert definition["comment_corpus"]["items"]["max_rows"]["default"] == 20000
        assert "只旁路已有 link/tree 响应，不新增请求" in definition["comment_corpus"]["items"]["enabled"]["hint"]

    candidate = service.snapshot()
    candidate["comment_corpus"].update({"enabled": True, "max_per_day": 501, "excluded_uids": ["123"]})
    assert await service.save(candidate) == {"comment_corpus"}
    assert fake_config["comment_corpus"] == candidate["comment_corpus"]
    assert ConfigService(fake_config).snapshot()["comment_corpus"] == candidate["comment_corpus"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "false"),
        ("collect_unknown_level", 1),
        ("min_level", -1),
        ("min_level", 101),
        ("max_per_day", -1),
        ("max_per_day", 100001),
        ("max_rows", -1),
        ("max_rows", 1000001),
        ("excluded_uids", "123"),
        ("excluded_uids", ["123", True]),
    ],
)
def test_comment_corpus_rejects_invalid_values(field, value) -> None:
    candidate = copy.deepcopy(DEFAULT_CONFIG)
    candidate["comment_corpus"][field] = value
    with pytest.raises(ConfigValidationError, match=field):
        ConfigService(candidate)


def test_legacy_unused_image_byte_limit_settings_are_dropped(fake_config) -> None:
    fake_config["context"]["max_image_size_mb"] = 8
    fake_config["context"]["max_total_image_size_mb"] = 24
    fake_config["retention"]["image_cache_soft_limit_mb"] = 200

    snapshot = ConfigService(fake_config).snapshot()

    assert "max_image_size_mb" not in snapshot["context"]
    assert "max_total_image_size_mb" not in snapshot["context"]
    assert "image_cache_soft_limit_mb" not in snapshot["retention"]


@pytest.mark.asyncio
async def test_config_save_uses_same_object_and_callback(fake_config) -> None:
    service = ConfigService(fake_config)
    changed_seen = []

    async def callback(changed):
        changed_seen.append(changed)

    service.add_restart_callback(callback)
    candidate = service.snapshot()
    candidate["reply"]["max_reply_chars"] = 450
    changed = await service.save(candidate)
    assert fake_config["reply"]["max_reply_chars"] == 450
    assert fake_config.save_count == 1
    assert changed == {"reply"}
    assert changed_seen == [{"reply"}]


@pytest.mark.parametrize('enabled', [True, False])
async def test_direct_reply_setting_survives_upgrade_save_and_reload(fake_config, enabled):
    fake_config['reply']['reply_to_direct_replies'] = enabled
    for field in ('reply_thread_once', 'reply_daily_per_user', 'reply_daily_total', 'reply_owner_exempt'):
        fake_config['reply'].pop(field)
    fake_config.pop('risk')
    fake_config['publishing'].pop('comment_daily_limit')
    service = ConfigService(fake_config)
    assert service.snapshot()['reply']['reply_to_direct_replies'] is enabled
    candidate = service.snapshot()
    candidate['publishing']['comment_daily_limit'] = 12
    assert await service.save(candidate) == {'publishing'}
    reloaded = ConfigService(fake_config)
    assert reloaded.snapshot()['reply']['reply_to_direct_replies'] is enabled
    assert reloaded.snapshot()['reply']['reply_daily_total'] == 30
    assert reloaded.ui_schema()['reply']['items']['reply_to_direct_replies']['default'] is True


@pytest.mark.asyncio
async def test_config_allows_explicit_unreviewed_proactive_send(fake_config) -> None:
    service = ConfigService(fake_config)
    candidate = copy.deepcopy(DEFAULT_CONFIG)
    candidate["proactive_feed"].update(
        {"enabled": True, "dry_run": False, "review_required": False}
    )
    changed = await service.save(candidate)
    assert changed == {"proactive_feed"}
    assert service.snapshot()["proactive_feed"] == candidate["proactive_feed"]


def test_config_rejects_duplicate_profile(fake_config) -> None:
    fake_config["profiles"].append(copy.deepcopy(fake_config["profiles"][0]))
    with pytest.raises(ConfigValidationError, match="重复"):
        ConfigService(fake_config)


def test_config_rejects_unsafe_storage_and_timeout_values(fake_config) -> None:
    fake_config["retention"]["database_warn_mb"] = 300
    fake_config["retention"]["database_soft_limit_mb"] = 200
    with pytest.raises(ConfigValidationError, match="软上限"):
        ConfigService(fake_config)

    safe = copy.deepcopy(DEFAULT_CONFIG)
    safe["network"]["request_timeout_seconds"] = 0
    with pytest.raises(ConfigValidationError, match="request_timeout_seconds"):
        ConfigService(safe)

    invalid_context = copy.deepcopy(DEFAULT_CONFIG)
    invalid_context["context"]["thread_reply_recent_comments"] = 0
    with pytest.raises(ConfigValidationError, match="thread_reply_recent_comments"):
        ConfigService(invalid_context)

    invalid_compression = copy.deepcopy(DEFAULT_CONFIG)
    invalid_compression["context"]["thread_reply_compressed_post_chars"] = 100
    with pytest.raises(ConfigValidationError, match="thread_reply_compressed_post_chars"):
        ConfigService(invalid_compression)

    invalid_image_budget = copy.deepcopy(DEFAULT_CONFIG)
    invalid_image_budget["context"]["image_total_timeout_seconds"] = 601
    with pytest.raises(ConfigValidationError, match="image_total_timeout_seconds"):
        ConfigService(invalid_image_budget)

    invalid_fallback_grace = copy.deepcopy(DEFAULT_CONFIG)
    invalid_fallback_grace["reply"]["provider_fallback_grace_seconds"] = 301
    with pytest.raises(ConfigValidationError, match="provider_fallback_grace_seconds"):
        ConfigService(invalid_fallback_grace)


def test_config_rejects_invalid_provider_ids() -> None:
    invalid_type = copy.deepcopy(DEFAULT_CONFIG)
    invalid_type["providers"]["llm_provider_id"] = 123
    with pytest.raises(ConfigValidationError, match="llm_provider_id"):
        ConfigService(invalid_type)

    too_long = copy.deepcopy(DEFAULT_CONFIG)
    too_long["providers"]["image_provider_id"] = "x" * 257
    with pytest.raises(ConfigValidationError, match="image_provider_id"):
        ConfigService(too_long)

    invalid_compressor = copy.deepcopy(DEFAULT_CONFIG)
    invalid_compressor["providers"]["context_provider_id"] = 123
    with pytest.raises(ConfigValidationError, match="context_provider_id"):
        ConfigService(invalid_compressor)


GUARD_FIELDS = {
    "reply": {
        "reply_thread_once": True, "reply_daily_per_user": 5,
        "reply_daily_total": 30, "reply_owner_exempt": True,
    },
    "publishing": {"comment_daily_limit": 30},
    "risk": {
        "enabled": True, "auto_filter_enabled": False, "watch_score": 20,
        "auto_filter_score": 60, "flood_window_minutes": 10, "flood_threshold": 8,
        "burst_score": 20, "repeat_score": 15, "injection_score": 25,
        "privacy_score": 30, "owner_exempt": True,
    },
}
GUARD_INTS = [
    (group, key) for group, fields in GUARD_FIELDS.items()
    for key, default in fields.items() if type(default) is int
]
GUARD_BOOLS = [
    (group, key) for group, fields in GUARD_FIELDS.items()
    for key, default in fields.items() if type(default) is bool
]


def test_guard_defaults_upgrade_legacy_config_and_match_schema(fake_config):
    for group, fields in GUARD_FIELDS.items():
        for key in fields:
            fake_config[group].pop(key)
    fake_config.pop("risk")
    service = ConfigService(fake_config)
    schema = service.ui_schema()
    raw_schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
    for group, fields in GUARD_FIELDS.items():
        for key, default in fields.items():
            assert service.snapshot()[group][key] == default
            assert service.defaults()[group][key] == default
            for definition in (schema, raw_schema):
                field = definition[group]["items"][key]
                assert field["default"] == default
                assert field["type"] == ("bool" if type(default) is bool else "int")


@pytest.mark.parametrize("group,key", GUARD_INTS)
@pytest.mark.parametrize("invalid", [None, True, False, 1.0, "5", -1, 100001])
def test_guard_integers_are_strict(group, key, invalid):
    candidate = copy.deepcopy(DEFAULT_CONFIG)
    candidate[group][key] = invalid
    with pytest.raises(ConfigValidationError, match=key):
        ConfigService(candidate)


@pytest.mark.parametrize("group,key", GUARD_BOOLS)
@pytest.mark.parametrize("invalid", [None, 0, 1, "true", [], {}])
def test_guard_booleans_are_strict(group, key, invalid):
    candidate = copy.deepcopy(DEFAULT_CONFIG)
    candidate[group][key] = invalid
    with pytest.raises(ConfigValidationError, match=key):
        ConfigService(candidate)


@pytest.mark.parametrize("group", GUARD_FIELDS)
@pytest.mark.parametrize("invalid", [None, [], False, ""])
def test_guard_groups_reject_explicit_non_objects(group, invalid):
    candidate = copy.deepcopy(DEFAULT_CONFIG)
    candidate[group] = invalid
    with pytest.raises(ConfigValidationError, match=group):
        ConfigService(candidate)


def test_guard_ranges_and_disabled_limits_match_schema():
    service = ConfigService(copy.deepcopy(DEFAULT_CONFIG))
    schema = service.ui_schema()
    for group, key in GUARD_INTS:
        field = schema[group]["items"][key]
        for boundary in (field["min"], field["max"]):
            candidate = copy.deepcopy(DEFAULT_CONFIG)
            candidate[group][key] = boundary
            if key == "watch_score":
                candidate["risk"]["auto_filter_score"] = boundary + 1
            elif key == "auto_filter_score":
                candidate["risk"]["watch_score"] = boundary - 1
            assert ConfigService(candidate).snapshot()[group][key] == boundary
        for invalid in (field["min"] - 1, field["max"] + 1):
            candidate = copy.deepcopy(DEFAULT_CONFIG)
            candidate[group][key] = invalid
            with pytest.raises(ConfigValidationError, match=key):
                ConfigService(candidate)
    for threshold in (20, 19):
        candidate = copy.deepcopy(DEFAULT_CONFIG)
        candidate["risk"]["auto_filter_score"] = threshold
        with pytest.raises(ConfigValidationError, match="必须大于"):
            ConfigService(candidate)


@pytest.mark.asyncio
async def test_guard_save_rejects_invalid_values_without_mutation(fake_config):
    service = ConfigService(fake_config)
    seen = []

    async def callback(changed):
        seen.append(changed)

    service.add_restart_callback(callback)
    previous = service.snapshot()
    for group, key, invalid in [("risk", "enabled", "false"), ("reply", "reply_daily_total", None),
                                ("publishing", "comment_daily_limit", True), ("risk", "auto_filter_score", 20)]:
        candidate = service.snapshot()
        candidate[group][key] = invalid
        with pytest.raises(ConfigValidationError):
            await service.save(candidate)
        assert service.snapshot() == previous
        assert fake_config.save_count == 0
        assert seen == []
    candidate = service.snapshot()
    candidate["risk"]["enabled"] = False
    candidate["reply"]["reply_daily_total"] = 0
    candidate["publishing"]["comment_daily_limit"] = 0
    assert await service.save(candidate) == {"risk", "reply", "publishing"}
    assert fake_config.save_count == 1
    assert seen == [{"risk", "reply", "publishing"}]
    assert service.snapshot() == candidate
