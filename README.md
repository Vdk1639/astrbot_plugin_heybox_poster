# astrbot_plugin_heybox_poster

AstrBot 小黑盒发帖插件：把「发动态 / 发长文 / 存草稿」注册成 LLM 函数工具，同时保留手动指令。

## 特性

- LLM 工具与手动指令双通道
- 支持动态（`dynamic`）与长文（`article`），以及草稿
- 可见范围：公开 / 粉丝可见 / 仅自己可见
- 话题搜索解析，或直接指定话题 ID
- 风控识别，命中 `show_captcha` 时提示更新 Cookie
- 白名单、冷却时间、`dry_run` 预演

## 安装

1. 把整个 `astrbot_plugin_heybox_poster/` 目录放到 AstrBot 的 `data/plugins/` 下；
2. 重启 AstrBot（或在 WebUI 插件页重载）；
3. 打开插件配置，填入 cookie。

### 获取 Cookie

发帖接口依赖两个值：

- `user_pkey`：登录态
- `x_xhh_tokenid`：风控令牌（缺失时返回 `show_captcha`）

三种拿法任选：

**A. 用 xiaoheihe-cli（推荐，可扫码登录并自动维护）**

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
| `allowed_users` | list | [] | 允许发帖的发送者白名单；空 = 不限制 |
| `cooldown_seconds` | int | 60 | 同一用户两次发帖的最小间隔；0 = 不限制 |
| `dry_run` | bool | true | 只回显表单、不真正提交；首次部署建议保持开启 |
| `request_timeout` | int | 20 | 单次请求超时（秒） |

## 使用

### LLM 工具（模型自行调用）

| 工具 | 作用 |
|---|---|
| `xhh_account_status()` | 查登录态、昵称、heybox_id |
| `xhh_search_topics(keyword)` | 搜社区/话题，拿 `topic_id` |
| `xhh_publish_post(title, text, topic, topic_ids, mode, draft)` | 发帖 / 存草稿 |

给模型的提示词里建议加一句：

> 发小黑盒前先用 `xhh_search_topics` 确认社区，话题必填；正文控制在 1000 字内。

### 手动指令

```text
/xhh状态
/xhh话题 Steam
/xhh发帖 标题|正文|Steam
```

## 许可

[MIT License](LICENSE)。本项目由 DeepSeek Harness 与 Codex 协助开发。
