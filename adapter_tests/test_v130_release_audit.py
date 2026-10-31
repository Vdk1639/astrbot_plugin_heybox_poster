from __future__ import annotations

import copy

from xiaoheihe.config_service import DEFAULT_CONFIG, ConfigService


def test_legacy_source_survives_astrbot_schema_default_prefill() -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["proactive_feed"]["source"] = "hardware"
    config["proactive_feed"]["fallback_sources"] = ["all"]

    snapshot = ConfigService(config).snapshot()

    assert snapshot["proactive_feed"]["fallback_sources"] == ["digital_tech"]
    assert "source" not in snapshot["proactive_feed"]


def test_new_fallback_selection_wins_over_stale_legacy_source() -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["proactive_feed"]["source"] = "game"
    config["proactive_feed"]["fallback_sources"] = ["anime", "digital_tech"]

    snapshot = ConfigService(config).snapshot()

    assert snapshot["proactive_feed"]["fallback_sources"] == ["anime", "digital_tech"]
    assert "source" not in snapshot["proactive_feed"]
