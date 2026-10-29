# Changelog

本文件记录插件各版本的功能变更，便于使用者判断是否需要升级。

## [v0.2.0] - 2026-10-29

### 新增

- 发帖幂等：本地 SQLite outbox，记录 `pending` / `sent` / `send_unknown` 状态，超时后可对账认领
- link_id 回查兜底：发帖前记录基线，只认新出现的条目，并按时间窗口过滤
- 删除能力：`delete_link` 与 `/xhh删除 <link_id>`，删除后自动清理 outbox
- 我的帖子列表：`xhh_my_posts` 工具与 `/xhh我的帖子` 指令
- 配置项：`view_limit`、`outbox_path`、`outbox_dedup_seconds`

### 修复

- 识别限流错误码 `10006`，抛出 `RateLimited`，提示稍后再试
- 修复去重窗口时间基准错误（改用 `updated_at`）
- `search_topics` 在本地按 limit 切片
- 按发送者加锁，串行化发帖写操作，避免并发重复发帖
- 错误文案按业务类型分类（缺参数 / 未登录 / 无权限 / 限流 / 风控 / 网络未知）

### 变更

- outbox 默认存储位置改为 AstrBot 插件数据目录，旧文件自动迁移

## [v0.1.0] - 2026-10-29

- 首个原型：发动态 / 长文 / 草稿，话题搜索、登录态检查、白名单、冷却、`dry_run` 预演
