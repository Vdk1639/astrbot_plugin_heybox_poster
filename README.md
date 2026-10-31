# 小黑盒（发帖与评论）

在一个 AstrBot 插件内管理小黑盒账号、发帖、评论、通知轮询和主动浏览，使用统一管理面板审核内容。

**本文对应 `dev` 整合开发版（Unreleased）**。版本文件仍为 **v0.6.0**，整合功能尚未发布为新版本；AstrBot 要求 **>=4.26.0,<5**。管理页依赖 `astrbot.api.web` 和已登录的 Dashboard 会话。

## 功能

- 多账号档案扫码登录、登录检查和安全退出；发帖与评论共用同一套凭据。
- 发布动态、长文和草稿，选择话题与可见范围；向帖子或指定楼层发表评论。
- 原生平台 **`heybox_community`**，机器人页面显示为 **小黑盒（发帖与评论）**，接入 AstrBot 会话、人格和 Agent 回复流程。
- 轮询 @ 提及及评论回复，按配置主动浏览分区或推荐流；统一发帖审核与评论审核。
- 管理面板包含状态、设置、浏览来源、事件、运行日志、SSE、诊断和存储清理；整合前的候选位于“历史主动评论审核”。
- 发布权限、冷却、持久审核队列、发送去重及未知结果保护。

## 安装与账号配置

1. 将本插件目录 `astrbot_plugin_heybox_poster` 放入 AstrBot 的 `data/plugins/`，安装 `requirements.txt` 后重载插件。
2. 打开 **AstrBot WebUI → 插件 → 小黑盒（发帖与评论）→ 可视化视图**，进入统一管理页。
3. 在“设置”的账号档案中添加账号，为每个档案指定不同的 `profile_id`，保存后在“扫码登录”选择对应档案扫码。默认档案为 `default`。
4. 在“发布编辑”选择发布账号，填写帖子或评论；按当前模拟运行与审核设置提交，在对应审核页处理。
5. 需要通知轮询或主动浏览时，在 **AstrBot 机器人页面**新增 **小黑盒（发帖与评论）**，类型为 `heybox_community`，将机器人的 `profile_id` 设置为已登录档案的同名值并启用。为需要运行这些任务的每个档案配置相应机器人。

**只需安装本插件，不需要第二个“小黑盒适配器”插件。** 仅通过面板或指令手动发布时，不要求创建轮询机器人。旧适配器使用的类型与 `heybox_community` 不同，旧机器人不会自动变成新机器人；迁移时停用同一小黑盒账号的旧轮询实例，避免两套插件或两个档案同时轮询、重复回复同一账号。

`profile_id` 是本地档案标识，不是小黑盒 UID，可使用字母、数字、下划线和短横线。默认发帖账号由 `publishing.profile_id` 决定；面板可单独选择其他档案，发帖及评论 LLM 工具也接受 `profile_id`。原生通知回复使用收到事件的机器人档案，不会改用默认发布账号。

## 模拟运行与审核

首次配置时，`publishing.dry_run`、每个档案的 `dry_run`、`publishing.post_review_required`、`publishing.comment_review_required` 均默认开启；`proactive_feed.enabled` 默认关闭，主动浏览的 `dry_run` 和 `review_required` 默认开启。

| 操作 | 实际发送前需关闭的模拟开关 | 默认审核入口 |
|---|---|---|
| 手动发帖、草稿、手动评论、通知评论 | `publishing.dry_run` 和所用 `profiles` 档案的 `dry_run` | 发帖审核 / 评论审核 |
| 主动浏览生成的评论 | 上述两项及 `proactive_feed.dry_run` | 评论审核 |
| `/xhh删除` | `publishing.dry_run` 和默认发布档案的 `dry_run` | 不进入发布审核队列，指令按权限直接执行 |

任何适用的模拟开关仍开启，都不会真实发送。关闭模拟运行不会自动批准已有待审内容；只有需要直接发送新内容时，才关闭对应的发布审核开关。主动评论还受 `proactive_feed.review_required` 约束，生成过程本身也可能要求审核。

- **已登录且开启审核：** 提交先返回 `pending`，即使正在模拟运行，也会进入审核队列。
- **模拟批准：** 返回 `dry_run`，未实际发送，审核项仍可能保持 `pending`。以刷新后的审核状态为准。
- **未登录且模拟运行：** 可返回预演结果而不生成审核项；真实发布前仍需扫码登录。
- **发送结果未知：** `send_unknown` 必须先在小黑盒核实，不能重新批准、拒绝或自动重发。后台中断时未完成的审核发送会保守转为未知状态。

审核项绑定提交时的档案和账号 UID。档案后来换成另一个账号时，不能用新账号发送旧账号的审核内容。

发帖与评论审核页各显示最近 100 条记录，包含全部状态。已发送、已拒绝、发送失败及其他非待审记录只读；明确失败会显示错误原因，发送结果未知则保留人工核对提示。

“历史主动评论审核”仅处理整合前的遗留候选。在该页面确认后，候选按当前档案 UID 绑定并转入统一评论审核，返回 `queued`，**不会直接发送**。还需在“评论审核”核对账号与内容，再次批准；模拟运行与审核规则仍生效。新生成的主动评论直接使用统一队列。

## 指令与 LLM 工具

原有指令格式保持兼容，使用 `publishing.profile_id` 指定的默认发布档案：

```text
/xhh登录
/xhh状态
/xhh话题 Steam
/xhh发帖 标题|正文|话题名或ID
/xhh我的帖子
/xhh删除 帖子ID
/xhh评论 帖子ID|评论正文
/xhh评论 帖子ID|回复正文|父评论ID
```

`/xhh发帖` 发布动态；配置 `publishing.default_topic_ids` 后第三段可省略。`/xhh评论` 第三段可省略，省略时评论主帖。需要逐次指定账号、长文、草稿或根评论 ID 时，使用面板或 LLM 工具。

| 工具 | 用途 |
|---|---|
| `xhh_account_status` | 检查默认发布账号登录态 |
| `xhh_search_topics` | 搜索社区 / 话题，最多返回 5 条 |
| `xhh_publish_post` | 动态、长文或草稿；可选 `profile_id`、`topic_ids`、`mode`、`draft` |
| `xhh_my_posts` | 查询默认发布账号最近的帖子 |
| `xhh_publish_comment` | 提交评论；参数含 `post_id`、`text`、可选 `parent_comment_id`、`root_comment_id`、`profile_id` |

例如：“用 `profile_id=work` 的账号，向帖子 `123456` 评论‘感谢分享’，提交审核。”确保模型已启用对应工具。发帖指令与工具优先使用明确话题 ID；名称搜索会选取首个匹配，可先搜索确认。面板话题输入按数字 ID 处理。

## 配置与浏览来源

统一面板的“设置”和 AstrBot 插件配置共用配置对象。现行发布配置位于 **`publishing` 分组**，旧版顶层发帖配置仅用于首次迁移。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `publishing.profile_id` | `default` | 未显式选择账号时使用的发布档案 |
| `publishing.dry_run` | `true` | 全局发布与删除模拟开关 |
| `publishing.post_review_required` | `true` | 帖子先提交审核 |
| `publishing.comment_review_required` | `true` | 手动评论和原生评论先提交审核 |
| `publishing.default_topic_ids` | 空字符串 | 默认话题 ID，多个用英文逗号分隔 |
| `publishing.view_limit` | `2` | `1` 公开 / `2` 粉丝可见 / `3` 仅自己可见 |
| `publishing.allowed_users` | `[]` | 手动指令与工具的写权限；留空仅管理员 |
| `publishing.cooldown_seconds` | `60` | 账号实际发送间隔；手动发帖另有发送者冷却 |
| `publishing.rate_limit_cooldown_seconds` | `600` | 命中限流后的持久冷却秒数 |
| `publishing.request_timeout` | `20` | 发帖请求超时秒数 |
| `publishing.outbox_path` | 空字符串 | 发帖 outbox 路径；留空使用插件数据目录 |
| `publishing.outbox_dedup_seconds` | `600` | 成功发送去重窗口；待处理或未知结果持续阻止重复提交 |
| `publishing.outbox_purge_days` | `7` | 启动时清理过期失败 outbox 记录；`0` 不清理 |

白名单可写为 `["qq:123", "xhh:456"]`；裸 ID 会匹配不同平台的同名 ID。显式配置白名单后按名单放行；账号管理页独立使用 Dashboard 鉴权，白名单不会授予面板权限。原生通知和主动浏览的用户、作者、关键词筛选位于 `permissions` 等分组。

在“浏览来源”选择真实分区，或配置推荐流回退分类。探测使用扫码页当前选择的档案；浏览来源配置由 `proactive_feed` 管理。真实分区优先；未选分区或所选分区本轮全部请求失败时使用回退分类。设置页还能配置轮询间隔、回复、模型、网络、日志和保留期限。

## 升级、凭据与接口权限

旧版发帖配置首次启动时迁入 `publishing`；设置迁移完成后写入不含凭据的 `settings_migrated.json`，避免凭据暂时不完整时重启覆盖新面板设置。完整旧凭据导入 `default` 档案。已有 `default` 凭据不会被旧配置覆盖。迁移时先保留 `legacy_login_backup.json`，成功保存配置后清空旧凭据字段，并写入不含凭据的 `integration_migrated.json`。标记存在后不再重复导入，避免登出后旧账号重新出现；不要为了重新登录而删除这些标记。

旧凭据或 UID 不完整时会保留旧配置并显示迁移提示，可先在 `default` 档案扫码，再重启完成迁移。该流程只处理本插件旧版配置，不自动搬运另一个适配器插件的数据目录。

新登录凭据按档案保存在插件数据目录的 `credentials/<profile_id>.json`。覆盖登录前保留对应 `.json.bak`，失败时回滚。安全退出需确认，清除所选档案当前凭据及其 `.json.bak`；迁移备份是独立文件，不属于此退出清理范围。凭据与备份含敏感信息，不要提交、分享或粘贴到诊断输出中。

面板只通过已认证的 AstrBot bridge 调用插件 Web API，路由前缀为 `/astrbot_plugin_heybox_poster/`。账号、设置、发布、审核、事件、日志 SSE、存储和诊断接口均要求 Dashboard 登录；批准审核、退出登录和执行存储清理还要求 `confirm: true`。这些接口不是匿名 webhook，也不提供绕过 Dashboard 的 webhook 密钥入口。`config` 与 `config/schema` 仅暴露现行配置分组，不返回旧凭据和迁移字段。

## 开发与验证

使用 Python 3.12、Node.js，在仓库根目录运行。以下均为离线模拟检查，不执行真实扫码、发帖或评论：

```sh
python -m pip install -r requirements.txt
python -m pip install pytest pytest-asyncio
python -m unittest discover -s tests -v
python -m pytest adapter_tests -q --asyncio-mode=auto
node tests/test_dashboard.mjs
git diff --check
```

通过这些检查不代表已完成目标 AstrBot 部署、真实扫码、通知轮询或发送的实机验收；待验收内容见 [TODO](TODO.md)。

[更新日志](CHANGELOG.md) · [维护指南](AGENTS.md) · [历史扫码诊断](QR_LOGIN_DIAGNOSIS.md)

本项目以 [MIT License](LICENSE) 发布。整合的“小黑盒适配器”原作者为 **RyanVaderAn**，其原生平台、运行时、面板与测试改编范围和完整许可见 [第三方声明](THIRD_PARTY_NOTICES.md)及[保留的上游 LICENSE](third_party/xiaoheihe-adapter.LICENSE)。
