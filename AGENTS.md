# 维护指南

本项目在 `dev` 整合发帖插件与小黑盒原生平台适配器。保持旧指令和工具参数兼容；现行账号管理统一使用多档案存储，升级整合版后需重新登录。当前版本为 v0.7.0。

## 模块边界

| 入口 | 职责 |
|---|---|
| `main.py` | 插件初始化、指令与 LLM 工具、手动操作权限、生命周期与 Agent 钩子转发 |
| `community.py` | 原生事件的模型路由、上下文、图片理解与回复聚合；改编自上游适配器入口 |
| `core/publishing.py` | 帖子与评论共用的档案选择、UID 校验、模拟开关、审核与发送入口 |
| `core/review.py` | `reviews.sqlite3` 持久队列、去重、原子领取和未知发送保护 |
| `core/dashboard.py` | 继承原生管理 API，新增发布审核接口及主动浏览候选转入统一审核 |
| `core/client.py`、`core/post.py`、`core/transport.py`、`core/sign.py` | 发帖业务、表单、签名请求及错误处理；旧导入路径继续兼容 |
| `core/outbox.py` | 原有发帖发送登记、去重、对账和持久冷却 |
| `xiaoheihe/auth.py` | 档案凭据存储、扫码、校验、覆盖备份与登出；这是统一登录入口 |
| `xiaoheihe/adapter.py`、`runtime.py`、`event.py` | `heybox_community` 原生平台、事件分发和运行时服务 |
| `xiaoheihe/notification_service.py`、`feed_service.py`、`task_manager.py` | 通知轮询、主动浏览及后台任务生命周期 |
| `xiaoheihe/config_service.py`、`web_api.py` | 配置校验、现行分组 schema、已认证管理 API |
| `pages/account/` | 完整管理面板；`bridge.js` 管认证与关闭，`publishing.js` 管编辑审核，`topic_probe.js` 管浏览来源 |
| `core/login.py`、`debug_qr.py` | 保留的独立扫码诊断客户端与工具；统一登录及管理 API 由 `xiaoheihe/auth.py` 与 `core/dashboard.py` 提供 |
| `tests/`、`adapter_tests/` | 本项目离线回归与改编的上游测试 |

插件标识和 API 前缀保持 `astrbot_plugin_heybox_poster`，原生平台类型为 `heybox_community`，显示名“小黑盒冲浪板”。机器人 `profile_id` 必须匹配账号档案；默认手动发布档案由 `publishing.profile_id` 选择，面板和发布工具可以显式覆盖。不要从同一账号启动新旧两个插件的轮询器。

## 必须保留的行为

- 手动发帖、评论和删除检查 `publishing.allowed_users` / AstrBot 管理员权限；面板入口使用 Dashboard 鉴权，不能借手动白名单绕过。
- 所有生产发布入口经过 `PublishingService`；原生通知回复和新主动候选进入同一审核队列，不能从 Agent 回调绕开审核直接调用客户端发送。
- 主动浏览候选的 `feed_approve` 由 `Dashboard` 覆盖：确认只将候选转入统一审核并绑定当前 UID，返回 `result: queued`，不直接发送；仍需在评论审核再次批准。
- `publishing.dry_run` 与档案 `dry_run` 做逻辑或；主动评论再叠加 `proactive_feed.dry_run`。真实发送前重新检查当前开关与凭据，不能只信任生成时的快照。
- 默认发帖和评论都需审核。有凭据且启用审核时提交返回 `pending`；模拟批准返回 `dry_run` 时审核项可能仍为 `pending`，无凭据的模拟提交可不生成审核项。
- 审核绑定提交时的 `profile_id` 和 `account_id`；生成期间、批准前及实际发送前不能因换号而改用另一 UID。
- 网络异常、任务取消和不确定响应保留未知结果；进程重启将遗留 `sending` 转为 `send_unknown`。不能将未知记录自动当成失败重发。发帖 outbox 的对账不代表所有评论审核状态都能自动修复。
- 只有 `pending` 可以批准或拒绝。审核页按类型读取最近 100 条全部状态，保留已发送、拒绝、失败等只读历史；明确失败不应提示为结果未知。页面显示账号、内容、目标和状态，未知结果不能提供重发入口；动态文本用安全 DOM，禁止插入不可信 HTML。
- Web API（含日志 SSE）要求已认证 Dashboard 会话；审核批准、登出和存储清理要求 JSON 布尔值 `confirm: true`。这些路由不是匿名 webhook。
- `config`、`config/defaults`、`config/schema` 只返回 `DEFAULT_CONFIG` 中的现行分组。接口与日志不得输出 Cookie、二维码响应原文或凭据备份。
- 扫码使用隔离会话和 `app=web`；覆盖账号前保留 `.json.bak`，失败回滚。统一面板卸载时清理轮询、SSE、动画和确认弹窗，迟到响应不能写回旧账号状态。
- 修改共享逻辑前搜索指令、工具、运行时、页面、配置和测试调用方。移动上游代码时保留校验、错误处理和归属声明；已有服务可复用时不增加平行业务层。

## 离线验证

Python 3.12 与 Node.js；测试使用 AstrBot 桩、模拟网络和临时数据库，无需真实账号。

```sh
python -m pip install -r requirements.txt
python -m pip install pytest pytest-asyncio
python -m unittest discover -s tests -v
python -m pytest adapter_tests -q --asyncio-mode=auto
node tests/test_dashboard.mjs
git diff --check
```

`adapter_tests` 使用异步 pytest fixture，显式设置 `--asyncio-mode=auto`，避免依赖上游仓库未搬入的 pytest 配置。新页面入口是 `tests/test_dashboard.mjs`；旧 `tests/test_account_page.mjs` 依赖已替换的单账号 DOM，目前保留作参考，不代表现行页面测试。替换 CI 入口前确认仍适用的轮询恢复等断言已迁移。

这些检查不证明真实扫码、发帖、评论、轮询或 AstrBot 宿主升级已通过。真实账号写操作及清除部署凭据须由用户明确授权，不在离线验证中执行。不要将凭据、二维码、日志或数据库提交到仓库。

## 版本与归属

本轮在 `CHANGELOG.md` 的 dev 条目记录整合变化，保留 `VERSION`、元数据版本、插件名和仓库地址。不要因 `xiaoheihe` 上游代码原有版本号而误报整合发布。

新增或搬运上游内容时同步维护 `THIRD_PARTY_NOTICES.md`，保留 `third_party/xiaoheihe-adapter.LICENSE` 原文，不把整合的原生平台、面板及测试描述为仅参考实现。
