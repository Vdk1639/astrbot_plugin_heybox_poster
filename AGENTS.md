# 维护指南

本项目是 AstrBot 小黑盒发帖插件。保持现有指令、LLM 工具、配置项和数据格式兼容，优先复用现有实现，不为重构增加运行依赖。

## 代码入口

| 文件 | 职责 |
|---|---|
| `main.py` | AstrBot 接入、权限、冷却和配置保存；指令与 LLM 工具共用 `_publish` |
| `core/client.py` | HTTP、响应校验、发布表单、发送登记与对账 |
| `core/outbox.py` | SQLite 原子去重、未知结果和持久冷却 |
| `core/login.py` | 隔离的扫码会话、凭据提取与身份校验 |
| `core/web.py` | Dashboard 鉴权、扫码任务和账号页面 API |
| `core/sign.py` | 请求签名；测试含抓包样本 |
| `pages/account/` | 通过 AstrBot bridge 访问 API 的账号页面 |
| `debug_qr.py` | 独立扫码诊断；不保存凭据 |

## 本地验证

使用 Python 3.12 和 Node.js，无需安装 AstrBot。先在虚拟环境安装依赖，再从仓库根目录运行与 CI 相同的检查：

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
node tests/test_account_page.mjs
git diff --check
```

Python 测试使用 AstrBot API 桩和模拟网络，Node.js 测试使用 DOM / bridge 桩。它们不证明真实扫码、发帖或宿主集成已经通过；专项验收见 `TODO.md`。

## 必须保留的行为

- 发帖、删除入口均检查权限；身份无法确认时拒绝写操作。`dry_run` 默认开启。
- HTTP 5xx、任务取消、网络及未知响应保留待对账记录，不能记成明确失败或自动重发。
- 去重基于账号与实际表单，区分话题、草稿、类型和可见范围；保留旧记录兼容和 SQLite 原子登记。
- 发布开始后捕获原账号标识，切换账号不改变这次请求的限流归属。
- 扫码仅在明确成功且凭据、账号 ID 校验通过后保存；保留旧凭据备份、保存失败回滚、卸载取消任务。
- 扫码请求使用 `app=web`；不要连带修改发帖客户端参数。日志与账号 API 不输出原始凭据或响应。
- 修改共享函数前搜索调用方，并同步检查命令、LLM 工具、页面、测试及配置说明。新增错误分支需要可复现的回归测试。

真实扫码、发布、删除和清除部署端凭据需要用户明确要求。离线测试不执行这些操作；不要将 Cookie、配置备份、二维码或数据库提交到仓库。
