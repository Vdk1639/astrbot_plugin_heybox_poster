from __future__ import annotations

import copy

from xiaoheihe.config_service import DEFAULT_CONFIG, ConfigService


def test_current_fallback_selection_is_preserved() -> None:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["proactive_feed"]["fallback_sources"] = ["anime", "digital_tech"]

    snapshot = ConfigService(config).snapshot()

    assert snapshot["proactive_feed"]["fallback_sources"] == ["anime", "digital_tech"]
