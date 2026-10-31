# 适配器测试迁移

此目录迁移自 `astrbot_plugin_xiaoheihe_adapter/tests`，与主仓库原 `tests` 分开维护。
从插件根目录运行：

```sh
python -m pytest adapter_tests -o asyncio_mode=auto -o asyncio_default_fixture_loop_scope=function
```

本地可使用 bundled Python，并将 `.venv/Lib/site-packages` 加入 `PYTHONPATH`。
Windows 沙箱下 pytest 创建的私有临时目录可能不可访问，此时需在正常权限的进程中运行测试。
测试只使用模拟服务和临时数据库，不执行真实扫码、发帖或评论。

## 保留的行为

- `test_main_contract.py` 改为 `test_community_agent.py`：加载 `community.py`，由 fixture 创建并关闭 runtime，再注入 `CommunityAgent`，保留原图预处理、身份归属、视觉缓存、Provider 回退、上下文压缩、Grok 工具隔离、Agent 生命周期和冷热启动测试。
- 新增协作类不注册插件、不创建第二 runtime/WebUI、不关闭注入 runtime、全部 8 个 hook 忽略旧平台、两种平台注册共存、热重载失败后继续处理和内部 session/extra 字段兼容测试。
- 原 `test_repository.py` 全部是数据库行为测试，无不适用的仓库元检查，全部保留。
- `test_v130_release_audit.py` 保留旧浏览来源迁移和新选择覆盖旧值的行为测试。
- stubs、helpers、fixtures 均保留，`tests.*` 引用改为 `adapter_tests.*`。平台注册断言使用 `PLATFORM_NAME`，Web API 路由断言使用 `PLUGIN_NAME`。
- `test_runtime.py` 明确关闭两种发布审核，继续检验原来的发送能力；整合后的默认审核由主仓库新增测试覆盖。
- 参考 `sanitize_reply_text` 已将评论换行合并为空格，迁移时修正其旧断言；仍验证工具内容过滤、最大长度和空回复拒绝。

## 未迁移的旧布局检查

以下 7 个测试依赖原仓库页面源码或旧根配置布局，整合后的 UI/schema 由主插件维护，因此不套用旧源码断言：

- `test_raw_schema_persists_browse_source_fields_without_generic_ui`
- `test_config_save_shows_success_toast`
- `test_plugin_page_uses_embedded_confirmation_for_candidate_approval`
- `test_plugin_page_defers_hidden_panel_requests_and_log_stream`
- `test_plugin_page_localizes_events_and_formats_times`
- `test_management_page_exposes_dedicated_browse_sources_ui`
- `test_browse_source_saves_reload_page_to_avoid_stale_settings_snapshot`

Web API 的鉴权、配置保存、Provider 选项和浏览来源探测行为测试仍保留。

## 主插件接线

主插件负责创建、绑定和关闭 runtime，创建 `CommunityAgent(context, runtime)`，调用其 `initialize()`，卸载时先调用 `terminate()` 清缓存，再关闭 runtime。协作类保留 `_provider_options()` 供主插件 WebUI 复用。

参考入口实际声明 8 个 hook，全部为返回 `None` 的异步方法；主插件显式注册以下装饰器并 `await` 转发：

| 原装饰器 | 方法（省略 self） |
| --- | --- |
| `on_waiting_llm_request(priority=1000)` | `prepare_xiaoheihe_before_agent(event: AstrMessageEvent)` |
| `on_llm_request()` | `inject_xiaoheihe_context(event: AstrMessageEvent, request: ProviderRequest)` |
| `on_using_llm_tool(priority=-1000)` | `isolate_xiaoheihe_images_for_grok(event: AstrMessageEvent, tool: object, tool_args: dict \| None)` |
| `on_llm_tool_respond(priority=1000)` | `restore_xiaoheihe_images_after_grok(event: AstrMessageEvent, tool: object, tool_args: dict \| None, tool_result: object \| None)` |
| `on_agent_done(priority=1000)` | `restore_xiaoheihe_images_on_agent_done(event: AstrMessageEvent, run_context: object, response: LLMResponse)` |
| `on_llm_response(priority=-1000)` | `capture_xiaoheihe_complete_reply(event: AstrMessageEvent, response: LLMResponse)` |
| `on_agent_begin(priority=1000)` | `mark_xiaoheihe_agent_started(event: AstrMessageEvent, run_context: object)` |
| `on_agent_done(priority=-1000)` | `mark_xiaoheihe_agent_done(event: AstrMessageEvent, run_context: object, response: LLMResponse)` |
