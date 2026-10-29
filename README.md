# astrbot_plugin_xhh_poster

AstrBot 小黑盒发帖插件：把「发动态 / 发长文 / 存草稿」注册成 LLM 函数工具，
同时保留手动指令。

与直接接 MCP / 子进程方案相比，这个插件是**单进程、零第三方依赖**的：
签名、表单、风控错误处理都在 `core/` 里，只依赖标准库。

## 为什么是插件而不是 MCP

| 方案 | 优点 | 代价 |
|---|---|---|
| **本插件（推荐）** | 单进程、可注册 LLM 工具、配置走 WebUI、失败信息直接回给模型 | 需要自己维护 cookie |
| 挂 MCP（xiaoheihe-cli / XiaoheiheMcpServer） | 复用成熟实现 | 多一个进程/端口，工具调用链更长，C# 版还要 Playwright |
| 子进程调 CLI | 实现最快 | 每次调用起进程，cookie 状态在外部，难做权限控制 |

## 功能

- `post_link` 发帖全链路：公共参数 + `hkey` 路径签名 + **Workshop `_rnd` 写签名** + 表单编码
- 两种形态：`dynamic`（动态，`link_tag=27`）/ `article`（长文，`link_tag=11`，自动补 `words_count`、`declaration` 等必填字段）
- 草稿：`draft=true`
- 话题解析：按名称搜索取第一个匹配，或直接给 `topic_ids`
- 风控识别：`status=show_captcha` → 明确提示更新 `x_xhh_tokenid`
- 白名单、冷却时间、`dry_run` 预演

## 安装

1. 把整个 `astrbot_plugin_xhh_poster/` 目录放到 AstrBot 的 `data/plugins/` 下；
2. 重启 AstrBot（或在 WebUI 插件页重载）；
3. 打开插件配置，填 cookie。

### 获取 cookie

必须同时具备两个值：

- `user_pkey`：登录态
- `x_xhh_tokenid`：风控令牌（缺失时接口返回 `{"status":"show_captcha"}`）

三种拿法，任选：

**A. 用 xiaoheihe-cli（推荐，能扫码登录并自动维护）**

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

| 键 | 说明 |
|---|---|
| `cookie` | 完整 Cookie；留空则用 `user_pkey` + `x_xhh_tokenid` 拼 |
| `heybox_id` | 账号 ID，作为公共参数发送 |
| `default_topic_ids` | 默认话题 ID，逗号分隔；模型没给话题时兜底 |
| `allowed_users` | 允许发帖的发送者白名单，空 = 不限制 |
| `cooldown_seconds` | 同一用户最小发帖间隔，默认 60 |
| `dry_run` | 默认 **true**：只回显表单不提交。确认字段无误后改成 false |

## 使用

### LLM 工具（模型自己调用）

| 工具 | 作用 |
|---|---|
| `xhh_account_status()` | 查登录态、昵称、heybox_id |
| `xhh_search_topics(keyword)` | 搜社区/话题，拿 `topic_id` |
| `xhh_publish_post(title, text, topic, topic_ids, mode, draft)` | 发帖/存草稿 |

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
astrbot_plugin_xhh_poster/
├── main.py               # Star 插件：指令 + LLM 工具 + 配置/白名单/频控
├── core/sign.py          # hkey + Workshop _rnd
├── core/client.py        # urllib 异步封装、表单构造、错误映射
├── _conf_schema.json     # WebUI 配置
├── metadata.yaml
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

本插件代码为独立实现，MIT。签名算法来自公开前端 bundle 的还原，仅供学习与个人使用；
请遵守小黑盒社区规则，控制发帖频率。
