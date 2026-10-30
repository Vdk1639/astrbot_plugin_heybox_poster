# astrbot_plugin_heybox_poster

AstrBot 小黑盒发帖插件：把「发动态 / 发长文 / 存草稿」注册成 LLM 函数工具，同时保留手动指令。

> ⚠️ **安全提示**：`allowed_users` 留空时**仅 AstrBot 管理员**可发帖或删除（管理员由 AstrBot 全局 `admins_id` 判定）；显式填写后则只允许白名单内的用户。无法确认管理员身份时拒绝写操作，请先配置好管理员或白名单。

## 特性

- LLM 工具与手动指令双通道
- 支持动态（`dynamic`）与长文（`article`），以及草稿
- 可见范围：公开 / 粉丝可见 / 仅自己可见
- 话题搜索解析，或直接指定话题 ID
- 风控识别，命中 `show_captcha` 时提示更新 Cookie
- 白名单、冷却时间、`dry_run` 预演
- WebUI 账号管理、扫码登录及切换；收到服务端确认和完整凭据后自动保存
- 限流冷却落盘，插件重载或进程重启后继续生效

## 安装

1. 把整个 `astrbot_plugin_heybox_poster/` 目录放到 AstrBot 的 `data/plugins/` 下；
2. 使用 AstrBot `>=4.24.2,<5`，安装插件的 `requirements.txt` 依赖并重启（或在 WebUI 插件页重载）；
3. 登录 AstrBot WebUI → 插件 → 小黑盒发帖 → 可视化视图 → 账号管理，扫码登录。也可在插件配置手动填写 Cookie。

升级到 v0.2.0 时，插件目录下旧的 `outbox.sqlite3` 会自动迁移到 AstrBot 插件数据目录，去重与对账记录保留。

### 获取 Cookie

**优先使用内置账号管理页**：点击「获取 / 刷新二维码」，用小黑盒 App 扫码并确认。页面分别显示当前账号登录态和扫码进度，并显示本次会话编号、成功查询次数和最后响应时间。后台每 3 秒查询一次，失败时按 2 / 4 / 8 / 10 秒退避重试，最长 120 秒；过期后手动刷新，请只扫页面当前显示的码。

当前插件使用一个发帖账号。再次扫码可以切换账号；只有服务端明确返回成功、取得 `user_pkey` 且账号 ID 唯一有效后才覆盖配置并重建客户端，失败时保留原账号。凭据既可来自 Cookie，也可来自 JSON 或嵌套的 user / account / profile / account_detail；缺少 `x_xhh_tokenid` 时按参考适配器公式生成客户端标识，设备标识随凭据保留。扫码保存不再以额外的 `restore_login` 请求作为前提。

扫码校验从新会话响应或 Cookie 取得 ID，不使用旧账号 ID；检查现有登录态仍调用 `/account/restore_login` 并携带配置里的 ID。打开页面时自动检查已配置的账号，网络或响应异常显示「暂时无法验证」，仅明确的失效响应才显示「登录已失效」。`ready` 只表示已扫码、等待服务端确认结果；手机已确认但接口一直返回 `ready` 时不会假报成功，请等待或过期后获取新码。未知扫码状态继续等待，单次查询失败不结束会话。日志和页面只显示固定环节、异常类型及重试次数，不输出原始响应或凭据。

「清除本地登录态」确认后会立即删除当前凭据和旧备份，需要重新扫码或填写 Cookie 才能恢复。直接调用 `auth/logout` 也必须发送 JSON `{"confirm": true}`；缺失、字符串或数字确认值均会被拒绝。

页面及 API 要求已登录的 AstrBot Dashboard 会话，通过内置 bridge 调用，不额外开放端口，不返回 Cookie。`/xhh登录` 为管理员提供页面入口提示。

覆盖前会在 AstrBot 插件数据目录保留一份 `login_backup.json`，每次覆盖替换上一份；清除登录态时删除备份。**插件配置和备份含明文登录凭据**，请限制数据目录的读取权限，勿分享或提交。备份不随自定义 `outbox_path` 移动。

以下为手动导入 Cookie 的替代方式。

发帖接口依赖两个值：

- `user_pkey`：登录态
- `x_xhh_tokenid`：风控令牌（缺失时返回 `show_captcha`）

三种拿法任选：

**A. 用 xiaoheihe-cli（可扫码登录并自动维护）**

```bash
uv tool install git+https://github.com/AaronZ345/xiaoheihe-cli.git
xiaoheihe-cli login          # 扫码
xiaoheihe-cli auth status    # 确认已登录
# 从会话文件取 cookie，或直接把 Cookie 头填进插件配置
```

**B. 浏览器复制**

登录 <https://www.xiaoheihe.cn> → DevTools → Network → 任意请求 →
复制请求头里的 `Cookie`，粘贴到插件配置 `cookie`（需包含 `user_pkey` 与 `x_xhh_tokenid`）。

**C. 56dz 仓库的 `get_token.js`**

```bash
node get_token.js --attach 9333   # 附着已登录的 Chrome
# 生成 cookie.json，把其中的 cookie 字段填进插件配置
```

## 配置项

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `cookie` | string | 空 | 完整 Cookie；留空则用 `user_pkey` + `x_xhh_tokenid` 拼接 |
| `user_pkey` | string | 空 | 登录态 Cookie 值 |
| `x_xhh_tokenid` | string | 空 | 风控 Cookie 值；缺失会返回 `show_captcha` |
| `heybox_id` | string | 空 | 当前账号 ID，作为公共参数发送 |
| `default_topic_ids` | string | 空 | 默认话题 ID（逗号分隔） |
| `view_limit` | int | 2 | 可见范围：1 公开 / 2 粉丝可见 / 3 仅自己可见 |
| `allowed_users` | list | [] | 如 `qq:123`、`xhh:456`；裸 ID 兼容；空 = 仅管理员 |
| `cooldown_seconds` | int | 60 | 同一用户两次发帖的最小间隔；0 = 不限制 |
| `dry_run` | bool | true | 发帖只回显表单，删除只回显帖子 ID；不执行写操作 |
| `request_timeout` | int | 20 | 单次请求超时（秒） |
| `outbox_path` | string | 空 | 发帖 outbox 数据库路径；留空则用 AstrBot 插件数据目录 |
| `outbox_dedup_seconds` | int | 600 | 同账号、同提交表单的成功发送去重窗口（秒）；未决任务持续阻止重发 |
| `rate_limit_cooldown_seconds` | int | 600 | 命中限流后按账号、平台发送者持久化冷却；0 禁用 |
| `outbox_purge_days` | int | 7 | 启动时清理多少天前的 failed 记录 |

`allowed_users` 的判定顺序：显式白名单 > 管理员（官方 `is_admin()` 优先，其次比对全局 `admins_id`）；无法确认权限时拒绝发帖和删除。

例如 `["qq:123", "xhh:456"]` 只放行 QQ 用户 123 和小黑盒用户 456。兼容别名 `aiocqhttp` / `qq_official` → `qq`、`heybox` / `xiaoheihe` → `xhh`；其他平台使用 AstrBot 的平台名。裸 ID `123` 会匹配各平台上的同名 ID，需要隔离时使用前缀。发帖白名单不授予账号页面权限。

发帖结果未知（网络中断、HTTP 5xx、响应异常）时保留 outbox 记录，禁止相同请求重发；重启时对账未知任务和中断遗留的 `pending`。无法确认的记录不会自动解除，请先人工核实帖子，勿盲目重试。草稿不会通过公开帖子列表认领。

去重按账号和完整表单区分草稿、正式发布、话题、类型及可见范围。填写 `heybox_id` 时用它标识账号；留空时使用 `user_pkey` 的散列，重新登录更换 `user_pkey` 后无法延续该账号标识，建议固定配置 `heybox_id`。升级前的记录没有账号信息，相同表单会保守继承到当前账号；升级时请保持原账号配置。

## 使用

### LLM 工具（模型自行调用）

| 工具 | 作用 |
|---|---|
| `xhh_account_status()` | 查登录态、昵称、heybox_id |
| `xhh_search_topics(keyword)` | 搜社区/话题，最多返回 5 条，拿 `topic_id` |
| `xhh_publish_post(title, text, topic, topic_ids, mode, draft)` | 发帖 / 存草稿 |
| `xhh_my_posts(limit)` | 只读查询当前账号最近的帖子列表 |

给模型的提示词里建议加一句：

> 发小黑盒时优先传明确的 `topic_ids`，未知时先用 `xhh_search_topics` 确认社区，避免同名社区误选；话题必填，正文控制在 1000 字内。

### 手动指令

```text
/xhh状态
/xhh登录
/xhh话题 Steam
/xhh发帖 标题|正文|Steam
/xhh删除 <link_id>
/xhh我的帖子
```

## 上游与协议

本项目以 [MIT License](LICENSE) 发布。实现过程中参考了以下公开项目：

| 项目 | 协议 | 用途 |
|---|---|---|
| [AaronZ345/xiaoheihe-cli](https://github.com/AaronZ345/xiaoheihe-cli) | MIT（© 2025 Li Fangyu） | `hkey` 签名变换、Workshop `_rnd` 公式、发帖表单字段、话题搜索接口 |
| [56dz/xiaoheihe-api](https://github.com/56dz/xiaoheihe-api) | GPL-3.0 | `hkey` 算法说明；`tests/` 中 6 组真实抓包签名向量（仅测试数据） |
| [ylty1516/xiaoheihe-agent](https://github.com/ylty1516/xiaoheihe-agent) | MIT（`pyproject.toml` 声明，仓库未附 LICENSE 文件） | 长文必填字段与发帖踩坑记录 |
| [674537331/astrbot_plugin_xiaoheihe_adapter](https://github.com/674537331/astrbot_plugin_xiaoheihe_adapter) | MIT（© 2026 RyanVaderAn） | WebUI / Dashboard 鉴权、扫码凭据解析与重试方案参考，改编客户端标识公式，许可见 THIRD_PARTY_NOTICES.md |

另评估但未采用代码：[wsmxd/XiaoheiheMcpServer](https://github.com/wsmxd/XiaoheiheMcpServer)（MIT，Playwright 方案）。

第三方许可原文与引用范围见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

离线回归与未完成的实机验证、P3 待办见 [TODO.md](TODO.md)。

本地检查（Python 3.12、Node.js 18+）：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
node tests/test_account_page.mjs
```

### 协议兼容性说明

- xiaoheihe-cli 为 MIT，与本项目 MIT 兼容；其版权与许可声明已完整保留在 `THIRD_PARTY_NOTICES.md`。
- 56dz/xiaoheihe-api 为 GPL-3.0。本项目**没有复制其源代码**，仅在 `tests/` 中使用 6 组输入/输出签名样本作回归校验；这类抓包数据不构成 GPL 代码的衍生作品，因此不影响本项目按 MIT 发布。
- 如需完全规避该引用：删除 `tests/test_core.py` 中的 `SAMPLES` 常量与 `test_hkey_matches_captured_samples` 用例（或替换为你自己抓取的样本）即可，其余测试不依赖它。

## 致谢

本项目由 [DeepSeek](https://www.deepseek.com/) 协助开发，并在 DeepSeek Harness 与 Codex 的支持下完成。

## 许可

本项目以 [MIT License](LICENSE) 发布，© 2026 Vdk1639。第三方组件与参考项目见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
