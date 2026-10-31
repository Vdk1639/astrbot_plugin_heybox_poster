from __future__ import annotations

import copy

import pytest

from xiaoheihe.config_service import DEFAULT_CONFIG, ConfigService, ConfigValidationError


def test_fallback_sources_accept_multiple_categories() -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["proactive_feed"]["fallback_sources"] = ["pc_game", "digital_tech", "anime"]

    snapshot = ConfigService(config).snapshot()

    assert snapshot["proactive_feed"]["fallback_sources"] == [
        "pc_game",
        "digital_tech",
        "anime",
    ]


@pytest.mark.parametrize(
    "sources, expected",
    [
        (["all", "pc_game"], ["all"]),
        (["pc_game", "pc_game"], ["pc_game"]),
    ],
)
def test_fallback_sources_canonicalize_redundant_selection(
    sources: list[str],
    expected: list[str],
) -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["proactive_feed"]["fallback_sources"] = sources

    snapshot = ConfigService(config).snapshot()

    assert snapshot["proactive_feed"]["fallback_sources"] == expected


@pytest.mark.parametrize(
    "sources, message",
    [
        ([], "至少需要一个"),
        (["not-a-source"], "不支持"),
    ],
)
def test_fallback_sources_reject_invalid_selection(sources: list[str], message: str) -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["proactive_feed"]["fallback_sources"] = sources

    with pytest.raises(ConfigValidationError, match=message):
        ConfigService(config)


def test_generic_schema_exposes_only_current_source_selectors() -> None:
    schema = ConfigService(copy.deepcopy(DEFAULT_CONFIG)).ui_schema()
    assert "source" not in schema["proactive_feed"]["items"]
