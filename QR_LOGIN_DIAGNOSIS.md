# 本机最小扫码验证记录

时间：2026-10-31，Asia/Shanghai。本次由用户配合手机扫描本机新生成的二维码并点击确认；直接访问官方接口，不经过 AstrBot 或 WebUI。

## 已定位的问题

v0.5.2 的扫码客户端复用发帖客户端公共参数，取码请求使用 `app=heybox`。网页登录应使用 `app=web`。只修正扫码取码的客户端类型，即可在本机成功拿到确认后的凭据；无需修改签名、浏览器 User-Agent 或发帖参数。

| 轮次 | 请求方式 | 实测结果 |
|---|---|---|
| 1：00:14:15–00:16:16 | v0.5.2 插件原请求 | 扫码后 `wait → ready`，用户确认后继续 `ready`，直到本地有效期结束；无凭据、账号 ID 或 Set-Cookie |
| 2：00:18:15–00:18:44 | 参考适配器的公共参数和请求头，二维码 `app=web` | `wait → ready → ok`；收到凭据与账号 ID |
| 3：00:20:21–00:20:47 | 插件原流程，只改扫码请求的 `app=web` | `wait → ok`；收到凭据与账号 ID，登录恢复接口返回 `status=ok` |

第二、三轮均收到两个 Set-Cookie：`user_pkey` 和 `user_heybox_id`；没有服务端下发的 `x_xhh_tokenid`，由 v0.5.2 已实现的参考公式补齐。凭据仅保留在进程内存中，用于只读验证，未输出实际值或写入插件配置。

两种签名实现按相同路径、时间戳和 nonce 对比，取码及轮询接口的 hkey 一致。第三轮保留插件其余参数与请求头，成功响应把问题进一步定位到扫码请求的 `app`。

第一轮没有进入成功分支，因此卡住发生在服务端返回成功之前。不能依据 `ready` 推断用户没点击确认，也不能通过把 `ready` 强行当成功来修复。

## 运行最小脚本

在插件目录、Python 3.11+ 且已安装本插件 requirements.txt 的环境运行：

```powershell
python debug_qr.py
# 用参考适配器的 GET 参数和请求头对照
python debug_qr.py --reference
```

每次运行都在 `.qr-debug/时间-随机编号/` 生成 `qr.png` 和 `diagnostic.jsonl`；请只扫描本次二维码。日志记录 HTTP 状态、Set-Cookie 数量、服务端已知状态标记、凭据存在性与失败阶段。未知状态值、服务端原始文案、二维码 URL、Cookie、pkey、token、昵称与实际账号 ID 均不写入日志。

二维码有效期最长 120 秒；按 Ctrl+C 可停止。不保存账号、不改插件配置、不发帖；目录已加入 Git 忽略。

## 如何判断

- `deadline_without_server_success`：有效期内未收到服务端成功，检查此前 `response` 中的状态和 `http` 事件。
- `failed` 且阶段为 `extract_cookie` / `extract_identity`：服务端报成功，但插件无法提取完整凭据或唯一有效 ID。
- `server_confirmed_credentials_extracted`：确认及凭据提取完成；仍应单独查看后面的验证接口响应。
- `verification` 是接口脱敏摘要，不等价于该接口通过了账号校验。此次 `/account/restore_login` 返回 `status=ok` 且有账号信息；`/bbs/app/api/user/permission` 响应没有 `status` 字段，显示 `<empty>`，不能据此判断登录失败。

## 已修复与待验证

v0.5.3 仅为 `QrLoginClient` 设置网页登录默认参数 `app=web`，不改发帖客户端、签名或现有账号配置。覆盖扫码请求默认参数、签名、参数透传及诊断脱敏的回归测试已加入。

原始脱敏日志仅保留在本地 `.qr-debug/` 目录，不随 Git 提交。

后续验收：用户于 2026-10-31 反馈目标 AstrBot 实机验收通过，v0.5.3 正式合并到主分支。此结论来自部署端用户反馈；上文三轮为本机独立验证记录。升级后应获取新码重新登录，原二维码不能用于验证修复结果。
