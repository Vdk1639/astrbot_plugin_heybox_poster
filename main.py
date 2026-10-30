"""AstrBot 小黑盒发帖插件。

给 LLM 提供三个函数工具（发帖 / 搜话题 / 查登录态），另带几个手动指令。
签名、表单编码、风控错误处理都在 ``core`` 里，这里只做 AstrBot 适配。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.client import (
    CaptchaRequired,
    NetworkError,
    RateLimited,
    XhhClient,
    XhhError,
    account_key,
    build_post_form,
)
from .core.outbox import Outbox

PLUGIN_NAME = "astrbot_plugin_heybox_poster"


@register(
    PLUGIN_NAME,
    "Vdk1639",
    "小黑盒发帖：LLM 工具 + 手动指令，支持动态/长文/草稿",
    "0.5.2",
)
class XhhPosterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._client: XhhClient | None = None
        self._client_cookie = ""
        self._last_post_at: dict[str, float] = {}
        self._outbox: Outbox | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self.web = None
        if callable(getattr(context, "register_web_api", None)):
            try:
                from .core.web import AccountWebUI
            except ModuleNotFoundError as exc:
                if exc.name != "astrbot.api.web":
                    raise
                logger.warning(f"[{PLUGIN_NAME}] 当前 AstrBot 不支持插件账号页面，请升级")
            else:
                self.web = AccountWebUI(self)
                self.web.register(context)

    # ------------------------------------------------------------------
    # 配置 / 客户端
    # ------------------------------------------------------------------
    def _c(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        return default if value is None else value

    def _view_limit(self) -> int:
        """可见范围：1 公开、2 粉丝可见、3 仅自己可见。"""
        try:
            value = int(self._c("view_limit", 2))
        except (TypeError, ValueError):
            value = 2
        return value if value in (1, 2, 3) else 2

    def _cookie(self) -> str:
        """优先用完整 cookie；否则用 pkey / tokenid 拼。"""
        raw = str(self._c("cookie", "") or "").strip()
        if raw:
            return raw
        parts = []
        pkey = str(self._c("user_pkey", "") or "").strip()
        token = str(self._c("x_xhh_tokenid", "") or "").strip()
        if pkey:
            parts.append("user_pkey=" + pkey)
        if token:
            parts.append("x_xhh_tokenid=" + token)
        return "; ".join(parts)

    def _get_client(self) -> XhhClient:
        cookie = self._cookie()
        heybox_id = str(self._c("heybox_id", "") or "").strip()
        if not cookie:
            raise XhhError("还没有配置 cookie：请在插件配置里填 cookie（或 user_pkey + x_xhh_tokenid）")
        if (self._client is None or cookie != self._client_cookie
                or heybox_id != self._client.heybox_id):
            if self._client is not None:
                # 旧客户端交给事件循环回收，这里不阻塞
                try:
                    asyncio.create_task(self._client.close())
                except Exception:
                    pass
            self._client = XhhClient(
                cookie,
                heybox_id=heybox_id,
                timeout=float(self._c("request_timeout", 20) or 20),
                outbox=self._get_outbox(),
            )
            self._client_cookie = cookie
        return self._client

    def _get_outbox(self) -> Outbox:
        if self._outbox is None:
            path = str(self._c("outbox_path", "") or "").strip()
            target = Path(path) if path else self._default_outbox_path()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            # 旧版本把 outbox 落在插件目录，迁移到新数据目录。
            legacy = Path(__file__).resolve().parent / "outbox.sqlite3"
            if target != legacy and legacy.exists() and not target.exists():
                for suffix in ("", "-wal", "-shm"):
                    src = Path(str(legacy) + suffix)
                    dst = Path(str(target) + suffix)
                    if src.exists():
                        try:
                            src.replace(dst)
                        except Exception:
                            pass
            dedup = int(self._c("outbox_dedup_seconds", 600) or 600)
            self._outbox = Outbox(str(target), dedup_seconds=dedup)
        return self._outbox

    def _default_outbox_path(self) -> Path:
        """outbox 默认落在 AstrBot 插件数据目录，避免升级覆盖插件目录。"""
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir()) / "outbox.sqlite3"
        except Exception:
            pass
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "outbox.sqlite3"
        except Exception:
            return Path(__file__).resolve().parent / "outbox.sqlite3"

    async def terminate(self) -> None:
        if self.web is not None:
            await self.web.close()
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._outbox is not None:
            self._outbox.close()
            self._outbox = None

    async def initialize(self) -> None:
        if not self._cookie():
            logger.warning(f"[{PLUGIN_NAME}] 未配置 cookie，发帖工具不可用")
            return
        try:
            status = await self._get_client().account_status()
            if status.get("logged_in"):
                logger.info(
                    f"[{PLUGIN_NAME}] 登录态正常：{status.get('nickname') or '?'}"
                    f"（{status.get('user_id') or '?'}）"
                )
            else:
                logger.warning(f"[{PLUGIN_NAME}] 登录态无效：cookie 可能已过期")
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 登录态检查失败：{type(exc).__name__}: {exc}")
        try:
            claimed = await self._get_client().reconcile_outbox()
            if claimed:
                logger.info(f"[{PLUGIN_NAME}] outbox 对账认领 {claimed} 条 send_unknown 帖子")
        except Exception:
            pass
        try:
            days = int(self._c("outbox_purge_days", 7) or 7)
            purged = self._get_outbox().purge(status="failed", older_than_days=days)
            if purged:
                logger.info(f"[{PLUGIN_NAME}] 清理 {purged} 条过期 failed 记录")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 权限 / 频控
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_ids(raw: Any) -> list[str]:
        """把白名单 / 管理员列表统一成干净的 ID 字符串列表。"""
        if isinstance(raw, str):
            raw = raw.replace("，", ",").split(",")
        if not raw:
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    def _allowed_users(self) -> list[str]:
        return self._normalize_ids(self._c("allowed_users", []))

    def _admin_ids(self) -> list[str]:
        """读取 AstrBot 全局管理员列表，取不到就返回空列表。"""
        try:
            config = self.context.get_config() if self.context is not None else {}
        except Exception:
            return []
        if not isinstance(config, dict):
            return []
        return self._normalize_ids(config.get("admins_id", []))

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """权限判断：显式白名单优先，否则默认仅管理员可用。"""
        allowed = self._allowed_users()
        if allowed:
            try:
                sender = str(event.get_sender_id())
            except Exception:
                return False
            return sender in allowed or self._sender(event) in allowed

        return self._is_admin(event)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """登录入口只认管理员，不使用发帖白名单。"""
        try:
            if event.is_admin():
                return True
        except Exception:
            pass

        admins = self._admin_ids()
        if admins:
            try:
                sender = str(event.get_sender_id())
            except Exception:
                return False
            return sender in admins

        return False

    def _check_cooldown(self, event: AstrMessageEvent) -> str:
        seconds = int(self._c("cooldown_seconds", 60) or 0)
        if seconds <= 0:
            return ""
        sender = self._sender(event)
        now = time.time()
        last = self._last_post_at.get(sender, 0)
        if now - last < seconds:
            wait = int(seconds - (now - last))
            return f"发帖太频繁了，请 {wait} 秒后再试。"
        self._last_post_at[sender] = now
        return ""

    def _sender(self, event: AstrMessageEvent) -> str:
        try:
            sender = str(event.get_sender_id())
        except Exception:
            return "unknown"
        try:
            platform = str(event.get_platform_name()).lower()
        except Exception:
            return sender
        platform = {
            "aiocqhttp": "qq", "qq_official": "qq",
            "heybox": "xhh", "xiaoheihe": "xhh",
        }.get(platform, platform)
        return f"{platform}:{sender}"

    def _lock_for(self, sender: str) -> asyncio.Lock:
        lock = self._locks.get(sender)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[sender] = lock
        return lock

    def _rate_limit_seconds(self) -> int:
        return max(0, int(self._c("rate_limit_cooldown_seconds", 600)))

    def _rate_limit_key(self, sender: str, account: str = "") -> str:
        account = account or account_key(self._cookie(), str(self._c("heybox_id", "") or "").strip())
        return f"{account}:{sender}"

    def _record_rate_limit(self, sender: str, account: str = "") -> None:
        seconds = self._rate_limit_seconds()
        if seconds:
            self._get_outbox().set_cooldown(self._rate_limit_key(sender, account), time.time() + seconds)

    def _rate_limit_block(self, sender: str) -> str:
        """命中限流后的本地冷却；返回空串表示不在冷却期。"""
        if not self._rate_limit_seconds():
            return ""
        until = self._get_outbox().cooldown_until(self._rate_limit_key(sender))
        if time.time() < until:
            wait = int(until - time.time()) + 1
            return f"刚触发限流，请 {wait} 秒后再试。"
        return ""

    def _friendly_error(self, exc: Exception) -> str:
        """按业务类型给出对用户友好的中文提示。"""
        if isinstance(exc, CaptchaRequired):
            return f"被风控验证码拦截：{exc}。请更新 cookie 里的 x_xhh_tokenid 后重试。"
        if isinstance(exc, RateLimited):
            return str(exc)
        if isinstance(exc, NetworkError):
            return f"发送结果未知（网络或服务端响应异常）：{exc}。请先核实结果，勿重复发送。"
        if isinstance(exc, XhhError):
            if exc.status in {"login", "relogin", "expired"}:
                return "登录态失效，请重新配置 cookie。"
            return str(exc)
        if isinstance(exc, ValueError):
            return f"参数错误：{exc}"
        return f"发帖失败：{type(exc).__name__}: {exc}"

    def _log_error(self, exc: Exception) -> None:
        """把原始错误码/msg 落日志，方便线上出问题时回溯。"""
        if isinstance(exc, XhhError):
            logger.warning(
                f"[{PLUGIN_NAME}] 发帖失败：{type(exc).__name__} "
                f"status={getattr(exc, 'status', None)} "
                f"payload={getattr(exc, 'payload', None)}"
            )

    # ------------------------------------------------------------------
    # 手动指令
    # ------------------------------------------------------------------
    @filter.command("xhh登录")
    async def cmd_login(self, event: AstrMessageEvent):
        """提示管理员从 Dashboard 进入账号管理页面。"""
        if not self._is_admin(event):
            yield event.plain_result("仅 AstrBot 管理员可以管理登录账号。")
            return
        yield event.plain_result(
            "请登录 AstrBot WebUI → 插件 → 小黑盒发帖 → 可视化视图 → 账号管理，扫码登录或切换账号。"
        )

    def _save_login(self, cookie: str, status: dict) -> None:
        from http.cookies import SimpleCookie

        parsed = SimpleCookie(cookie)
        if (not parsed.get("user_pkey") or not parsed["user_pkey"].value
                or not parsed.get("x_xhh_tokenid") or not parsed["x_xhh_tokenid"].value
                or not str(status.get("user_id") or "").isascii()
                or not str(status.get("user_id") or "").isdigit()):
            raise XhhError("登录凭据或账号 ID 无效，原登录态未替换")
        values = {
            "cookie": cookie, "user_pkey": parsed["user_pkey"].value,
            "x_xhh_tokenid": parsed["x_xhh_tokenid"].value, "heybox_id": str(status["user_id"]),
        }
        self._save_credentials(values)

    def _save_credentials(self, values: dict, *, backup: bool = True) -> None:
        old = {key: self._c(key, "") for key in values}
        save = getattr(self.config, "save_config", None)
        if not callable(save):
            raise XhhError("宿主不支持保存插件配置，原登录态未替换")
        self._get_outbox()
        # 提前检查客户端配置，避免凭据写入后才发现无法重建客户端。
        float(self._c("request_timeout", 20) or 20)
        backup_path = self._default_outbox_path().parent / "login_backup.json"
        if backup and any(old.values()):
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=backup_path.parent, delete=False
                ) as file:
                    temporary = Path(file.name)
                    json.dump(old, file, ensure_ascii=False)
                temporary.replace(backup_path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        self.config.update(values)
        try:
            save()
        except Exception:
            self.config.update(old)
            try:
                save()
            except Exception:
                logger.warning(f"[{PLUGIN_NAME}] 登录配置回滚失败，请检查配置文件权限")
            raise XhhError("登录配置保存失败，内存中的原登录态已恢复") from None
        if self._client is not None:
            asyncio.create_task(self._client.close())
            self._client = None
        self._client_cookie = ""
        if self._cookie():
            self._get_client()
        if not backup:
            backup_path.unlink(missing_ok=True)

    @filter.command("xhh状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看小黑盒登录态。"""
        try:
            status = await self._get_client().account_status()
        except Exception as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return
        if status.get("logged_in"):
            yield event.plain_result(
                f"已登录：{status.get('nickname') or '（无昵称）'}"
                f"（heybox_id={status.get('user_id') or '?'}）"
            )
        else:
            yield event.plain_result("未登录或 cookie 已失效，请重新配置 cookie（需含 user_pkey 与 x_xhh_tokenid）")

    @filter.command("xhh发帖")
    async def cmd_post(self, event: AstrMessageEvent):
        """手动发帖：/xhh发帖 标题|正文|话题名或ID"""
        if not self._is_allowed(event):
            yield event.plain_result("你没有发帖权限。请配置 AstrBot 管理员或插件 allowed_users 白名单。")
            return
        raw = event.message_str.strip()
        for prefix in ("/xhh发帖", "xhh发帖"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            yield event.plain_result("用法：/xhh发帖 标题|正文|话题名或ID（第三段可省略，用默认话题）")
            return

        title, text = parts[0], parts[1]
        topic = parts[2] if len(parts) > 2 else ""
        async with self._lock_for(self._sender(event)):
            block = self._rate_limit_block(self._sender(event))
            if block:
                yield event.plain_result(block)
                return
            wait = self._check_cooldown(event)
            if wait:
                yield event.plain_result(wait)
                return

            rate_account = account_key(self._cookie(), str(self._c("heybox_id", "") or "").strip())
            dry_run = bool(self._c("dry_run", False))
            try:
                client = None if dry_run else self._get_client()
                topic_ids = await self._resolve_topics(topic)
                view_limit = self._view_limit()
                if dry_run:
                    form = build_post_form(
                        title, text, topic_ids=topic_ids, view_limit=view_limit
                    )
                    yield event.plain_result(f"[dry-run] 将提交：{form}")
                    return
                result = await client.publish(
                    title,
                    text,
                    mode="dynamic",
                    topic_ids=topic_ids,
                    view_limit=view_limit,
                )
                if result.get("duplicate"):
                    yield event.plain_result(
                        f"相同内容在去重窗口内已发送过，link_id={result.get('link_id') or '（未知）'}"
                    )
                else:
                    yield event.plain_result(
                        f"发布成功，link_id={result.get('link_id') or '（未返回）'}"
                    )
            except Exception as exc:
                if isinstance(exc, RateLimited):
                    self._record_rate_limit(self._sender(event), rate_account)
                self._log_error(exc)
                yield event.plain_result(self._friendly_error(exc))

    @filter.command("xhh话题")
    async def cmd_topics(self, event: AstrMessageEvent):
        """搜话题/社区 ID：/xhh话题 Steam"""
        keyword = event.message_str.strip()
        for prefix in ("/xhh话题", "xhh话题"):
            if keyword.startswith(prefix):
                keyword = keyword[len(prefix):].strip()
                break
        if not keyword:
            yield event.plain_result("用法：/xhh话题 关键词")
            return
        try:
            topics = await self._get_client().search_topics(keyword)
        except Exception as exc:
            yield event.plain_result(f"搜索失败：{exc}")
            return
        if not topics:
            yield event.plain_result("没搜到相关话题。")
            return
        lines = [f"{t['topic_id']}  {t['name']}" for t in topics[:5]]
        yield event.plain_result("话题 ID / 名称：\n" + "\n".join(lines))

    @filter.command("xhh删除")
    async def cmd_delete(self, event: AstrMessageEvent):
        """删除帖子：/xhh删除 <link_id>（仅白名单用户）。"""
        if not self._is_allowed(event):
            yield event.plain_result("你没有删除权限。请配置 AstrBot 管理员或插件 allowed_users 白名单。")
            return
        raw = event.message_str.strip()
        for prefix in ("/xhh删除", "xhh删除"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        link_id = raw.strip()
        if not link_id:
            yield event.plain_result("用法：/xhh删除 <link_id>")
            return
        if self._c("dry_run", False):
            yield event.plain_result(f"[dry-run] 未真正删除，link_id={link_id}")
            return
        try:
            result = await self._get_client().delete_link(link_id)
            yield event.plain_result(
                f"已删除 link_id={link_id}，result={result}"
            )
        except Exception as exc:
            yield event.plain_result(self._friendly_error(exc))

    @filter.command("xhh我的帖子")
    async def cmd_my_posts(self, event: AstrMessageEvent):
        """只读查看我的帖子列表。"""
        try:
            posts = await self._get_client().list_my_posts(limit=20)
        except Exception as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return
        if not posts:
            yield event.plain_result("当前账号没有查到帖子。")
            return
        lines = "\n".join(f"- link_id={p['link_id']}  {p['title']}" for p in posts)
        yield event.plain_result("我的帖子：\n" + lines)

    # ------------------------------------------------------------------
    # LLM 函数工具
    # ------------------------------------------------------------------
    @filter.llm_tool(name="xhh_account_status")
    async def tool_status(self, event: AstrMessageEvent) -> str:
        """查询当前小黑盒账号是否已登录、昵称和 heybox_id。"""
        try:
            status = await self._get_client().account_status()
        except Exception as exc:
            return f"查询失败：{exc}"
        if status.get("logged_in"):
            return f"已登录，昵称={status.get('nickname') or '?'}，heybox_id={status.get('user_id') or '?'}"
        return "未登录或 cookie 已失效，无法发帖。"

    @filter.llm_tool(name="xhh_search_topics")
    async def tool_topics(self, event: AstrMessageEvent, keyword: str) -> str:
        """按关键词搜索最多 5 个小黑盒社区/话题；发帖优先传明确的 topic_id。

        Args:
            keyword(string): 社区或话题名称关键词，例如 "Steam"、"黑神话"
        """
        try:
            topics = await self._get_client().search_topics(keyword)
        except Exception as exc:
            return f"搜索失败：{exc}"
        if not topics:
            return f"没有搜到与「{keyword}」相关的话题。"
        return "候选话题：\n" + "\n".join(
            f"- {t['name']}（topic_id={t['topic_id']}）" for t in topics[:5]
        )

    @filter.llm_tool(name="xhh_my_posts")
    async def tool_my_posts(self, event: AstrMessageEvent, limit: int = 10) -> str:
        """只读查询当前账号最近发布的帖子列表，运维自查用。

        Args:
            limit(int): 最多返回条数，默认 10
        """
        try:
            posts = await self._get_client().list_my_posts(limit=limit)
        except Exception as exc:
            return f"查询失败：{exc}"
        if not posts:
            return "当前账号没有查到帖子。"
        return "\n".join(f"- link_id={p['link_id']}  {p['title']}" for p in posts)

    @filter.llm_tool(name="xhh_publish_post")
    async def tool_publish(
        self,
        event: AstrMessageEvent,
        title: str,
        text: str,
        topic: str = "",
        topic_ids: str = "",
        mode: str = "dynamic",
        draft: bool = False,
    ) -> str:
        """发布一条小黑盒帖子（默认发动态；也可发长文或存草稿）。

        调用前请确认标题、正文已写好；话题必填，优先传 topic_ids，避免同名社区误选。

        Args:
            title(string): 标题，不超过 100 字
            text(string): 正文纯文本；mode=article 时可传简单 HTML
            topic(string): 社区/话题名称，例如 "Steam"，会现场搜索取第一个匹配
            topic_ids(string): 已知的话题 ID，多个用英文逗号分隔；与 topic 二选一即可
            mode(string): dynamic（动态，默认）或 article（长文）
            draft(boolean): 为 true 时只存草稿，不公开发布
        """
        if not self._is_allowed(event):
            return "当前用户没有发帖权限。请配置 AstrBot 管理员或插件 allowed_users 白名单。"
        async with self._lock_for(self._sender(event)):
            block = self._rate_limit_block(self._sender(event))
            if block:
                return block
            wait = self._check_cooldown(event)
            if wait:
                return wait
            rate_account = account_key(self._cookie(), str(self._c("heybox_id", "") or "").strip())
            dry_run = bool(self._c("dry_run", False))
            try:
                client = None if dry_run else self._get_client()
                ids = await self._resolve_topics(topic, topic_ids)
                view_limit = self._view_limit()
                if dry_run:
                    form = build_post_form(
                        title,
                        text,
                        mode=mode,
                        topic_ids=ids,
                        draft=draft,
                        view_limit=view_limit,
                    )
                    return f"[dry-run] 未真正提交，表单如下：{form}"
                result = await client.publish(
                    title,
                    text,
                    mode=mode,
                    topic_ids=ids,
                    draft=draft,
                    view_limit=view_limit,
                )
            except Exception as exc:
                if isinstance(exc, RateLimited):
                    self._record_rate_limit(self._sender(event), rate_account)
                self._log_error(exc)
                return self._friendly_error(exc)

        if result.get("duplicate"):
            return (
                f"相同内容已在去重窗口内发送过，link_id={result.get('link_id') or '（未知）'}。"
            )
        link_id = result.get("link_id") or "（服务端未返回）"
        if draft:
            return f"草稿已保存，link_id={link_id}。"
        return f"发布成功，link_id={link_id}。"

    # ------------------------------------------------------------------
    async def _resolve_topics(self, topic: str = "", raw_ids: str = "") -> list[str]:
        """话题解析优先级：显式 ID > 名称搜索 > 配置默认值。"""
        ids = [x.strip() for x in str(raw_ids or "").replace("，", ",").split(",") if x.strip()]
        if ids:
            return ids

        keyword = str(topic or "").strip()
        if keyword:
            if keyword.isdigit():
                return [keyword]
            found = await self._get_client().search_topics(keyword)
            if not found:
                raise XhhError(f"没有找到话题「{keyword}」，请先用 xhh_search_topics 确认")
            return [found[0]["topic_id"]]

        defaults = str(self._c("default_topic_ids", "") or "")
        ids = [x.strip() for x in defaults.replace("，", ",").split(",") if x.strip()]
        if not ids:
            raise XhhError("缺少话题：请传 topic / topic_ids，或在插件配置里设置 default_topic_ids")
        return ids
