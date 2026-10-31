from __future__ import annotations


from adapter_tests.astrbot_stubs import REQUEST, Query
from xiaoheihe.config_service import ConfigService
from xiaoheihe.runtime import PLUGIN_NAME
from xiaoheihe.web_api import WebApiController


class FakeContext:
    def __init__(self) -> None:
        self.routes = []

    def register_web_api(self, route, handler, methods, description):
        self.routes.append((route, handler, methods, description))


class FakeRuntime:
    def __init__(self, config) -> None:
        self.config = ConfigService(config)


def test_registers_only_plugin_prefixed_routes(fake_config) -> None:
    controller = WebApiController(FakeRuntime(fake_config))
    context = FakeContext()
    controller.register(context)
    assert len(context.routes) >= 15
    assert all(
        route.startswith(f"/{PLUGIN_NAME}/") for route, _, _, _ in context.routes
    )


async def test_config_api_reads_and_saves_same_object(fake_config) -> None:
    runtime = FakeRuntime(fake_config)
    controller = WebApiController(runtime)
    REQUEST.username = "admin"
    response = await controller.config_get()
    assert response["status_code"] == 200
    payload = response["json"]
    payload["reply"]["max_reply_chars"] = 321
    REQUEST._json = payload
    saved = await controller.config_save()
    assert saved["status_code"] == 200
    assert fake_config["reply"]["max_reply_chars"] == 321
    assert fake_config.save_count == 1


async def test_config_schema_api_exposes_structured_form_metadata(fake_config) -> None:
    controller = WebApiController(FakeRuntime(fake_config))
    REQUEST.username = "admin"
    response = await controller.config_schema()
    assert response["status_code"] == 200
    schema = response["json"]
    assert schema["profiles"]["type"] == "template_list"
    assert schema["polling"]["items"]["poll_interval_seconds"]["type"] == "int"
    assert schema["providers"]["items"]["llm_provider_id"]["_special"] == ("select_provider")
    assert schema["providers"]["items"]["image_provider_id"]["_special"] == ("select_provider")
    assert schema["providers"]["items"]["context_provider_id"]["_special"] == ("select_provider")
    assert schema["logging"]["items"]["level"]["options"] == [
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
    ]


async def test_config_schema_includes_configured_provider_choices(fake_config) -> None:
    controller = WebApiController(
        FakeRuntime(fake_config),
        provider_supplier=lambda: [
            {"value": "provider-a", "label": "provider-a · test-model"},
            {"value": "provider-b", "label": "provider-b"},
        ],
    )
    REQUEST.username = "admin"
    schema = (await controller.config_schema())["json"]
    options = schema["providers"]["items"]["llm_provider_id"]["options"]
    assert options == [
        {"value": "", "label": "跟随当前配置"},
        {"value": "provider-a", "label": "provider-a · test-model"},
        {"value": "provider-b", "label": "provider-b"},
    ]
    assert schema["providers"]["items"]["context_provider_id"]["options"] == options


async def test_config_api_rejects_unknown_group(fake_config) -> None:
    controller = WebApiController(FakeRuntime(fake_config))
    REQUEST.username = "admin"
    REQUEST._json = {"unexpected": {}}
    response = await controller.config_save()
    assert response["status_code"] == 400


async def test_config_api_rejects_non_object_body(fake_config) -> None:
    controller = WebApiController(FakeRuntime(fake_config))
    REQUEST.username = "admin"
    REQUEST._json = []
    response = await controller.config_save()
    assert response["status_code"] == 400


async def test_web_api_can_manually_clear_auxiliary_provider_cooldown(fake_config) -> None:
    class CooldownRuntime(FakeRuntime):
        def __init__(self, config) -> None:
            super().__init__(config)
            self.cleared = None

        def clear_auxiliary_provider_cooldown(self, profile_id, purpose, provider_id):
            self.cleared = (profile_id, purpose, provider_id)
            return True

    runtime = CooldownRuntime(fake_config)
    controller = WebApiController(runtime)
    REQUEST.username = "admin"
    REQUEST._json = {
        "profile_id": "default",
        "purpose": "image",
        "provider_id": "vision-fixed",
    }

    response = await controller.clear_provider_cooldown()

    assert response["status_code"] == 200
    assert response["json"] == {"cleared": True}
    assert runtime.cleared == ("default", "image", "vision-fixed")


async def test_web_api_requires_dashboard_identity(fake_config) -> None:
    controller = WebApiController(FakeRuntime(fake_config))
    REQUEST.username = None
    response = await controller.config_get()
    assert response["status_code"] == 401
    REQUEST.username = "admin"
    REQUEST.query = Query({})
