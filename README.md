# astrbot_plugin_heybox_poster

AstrBot 小黑盒发帖插件：把「发动态 / 发长文 / 存草稿」注册成 LLM 函数工具，同时保留手动指令。签名、表单构造与风控处理全部内置，单进程运行、不额外起服务。

## 特性

- **单进程**：不依赖 MCP / CLI 子进程，配置走 AstrBot WebUI
- **零第三方依赖**：`core/` 只依赖 Python 标准库（`urllib`、`hashlib`、`hmac`），不与 AstrBot 自带的 `httpx` / `aiohttp` 版本冲突
- **双通道**：LLM 函数工具 + 手动指令
- 支持 `dynamic`（动态）与 `article`（长文）两种形态，以及草稿
- **可见范围**：公开 / 粉丝可见 / 仅自己可见（`view_limit`）
- 话题搜索解析：按名称取第一个匹配，或直接给 `topic_ids`
- 风控识别：`status=show_captcha` 时明确提示更新 `x_xhh_tokenid`
- 白名单、冷却时间、`dry_run` 预演

## 为什么是插件而不是 MCP

| 方案 | 优点 | 代价 |
|---|---|---|
| **本插件（推荐）** | 单进程、可注册 LLM 工具、配置走 WebUI、失败信息直接回给模型 | 需要自己维护 cookie |
| 挂 MCP（xiaoheihe-cli / XiaoheiheMcpServer） | 复用成熟实现 | 多一个进程/端口，工具调用链更长，C# 版还要 Playwright |
| 子进程调 CLI | 实现最快 | 每次调用起进程，cookie 状态在外部，难做权限控制 |

## 安装

1. 把整个 `astrbot_plugin_heybox_poster/` 目录放到 AstrBot 的 `data/plugins/` 下；
2. 重启 AstrBot（或在 WebUI 插件页重载）；
3. 打开插件配置，填入 cookie 与相关字段。

### 获取 Cookie

发帖接口同时依赖两个值：

- `user_pkey`：登录态
- `x_xhh_tokenid`：风控令牌（缺失时接口返回 `{"status":"show_captcha"}`）

三种拿法任选：

**A. 用 xiaoheihe-cli（推荐，可扫码登录并自动维护）**

```bash
uv tool install git+https://github.com/AaronZ345/xiaoheihe-cli.git
xiaoheihe-cli login          # 扫码
xiaoheihe-cli auth status    # 确认已登录
# 从它的会话文件里取 cookie，或直接把 Cookie 头填进插件配置
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
| `user_pkey` | string | 空 | 登录态 Cookie 值（`cookie` 留空时使用） |
| `x_xhh_tokenid` | string | 空 | 风控 Cookie 值；缺失会返回 `show_captcha` |
| `heybox_id` | string | 空 | 当前账号 ID，作为公共参数发送 |
| `default_topic_ids` | string | 空 | 默认话题 ID（逗号分隔），模型未指定话题时兜底 |
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

## 目录结构

```text
astrbot_plugin_heybox_poster/
├── main.py               # Star 插件：指令 + LLM 工具 + 配置/白名单/频控
├── core/sign.py          # hkey + Workshop _rnd 签名
├── core/client.py        # urllib 异步封装、表单构造、错误映射
├── _conf_schema.json     # WebUI 配置
├── metadata.yaml
├── LICENSE
└── tests/                # 离线单测（不需要 AstrBot / 不联网）
```

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：6 组真实抓包 hkey 向量、路径规范化、`_rnd` 形状、动态/长文表单必填项、
写请求带签名与表单编码、`show_captcha` / 业务错误 / 网络错误映射、
配置拼装、白名单、冷却、话题解析优先级。

## 验证边界（重要）

- 单测只验证「签名算法 + 表单结构 + 错误映射」，用 mock 拦截了网络；
- **没有用真实账号实际发过帖**，上线前请保持 `dry_run=true` 跑一遍，核对表单；
- 接口字段来自公开前端 bundle 的逆向结果，平台改版后可能失效；
- 发帖是公开写操作：建议开启 `allowed_users` 白名单与冷却，避免模型误触发。

## 许可

[MIT License](LICENSE)。签名算法来自公开前端 bundle 的还原，仅供学习与个人使用；请遵守小黑盒社区规则，控制发帖频率。
