"""AstrBot 小黑盒发帖插件。

提供统一账号、发帖评论、审核面板与原生 AstrBot 社区事件接入。
签名、表单编码、风控错误处理都在 ``core`` 里，这里只做 AstrBot 适配。
"""

from __future__ import annotations

import asyncio
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
)
from .core.outbox import Outbox
from .core.publishing import PublishingService
from .community import CommunityAgent
from .xiaoheihe.runtime import RuntimeServices, bind_runtime, unbind_runtime
from astrbot.api.provider import LLMResponse, ProviderRequest

PLUGIN_NAME = "astrbot_plugin_heybox_poster"
PLUGIN_VERSION = Path(__file__).with_name("VERSION").read_text(encoding="ascii").strip()


@register(
    PLUGIN_NAME,
    "Vdk1639",
    "小黑盒社区：多账号登录、发帖评论、轮询与统一审核",
    PLUGIN_VERSION,
)
class XhhPosterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._client: XhhClient | None = None
        self._clients: dict[str, XhhClient] = {}
        self._last_post_at: dict[str, float] = {}
        self._outbox: Outbox | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        data_dir = self._default_outbox_path().parent
        self.runtime = RuntimeServices(config, data_dir)
        bind_runtime(self.runtime)
        self.publisher = PublishingService(self.runtime, lambda profile: self._get_client(profile), self._get_outbox)
        self.runtime.publisher = self.publisher
        self.community = CommunityAgent(context, self.runtime)
        self.web = None
        if callable(getattr(context, "register_web_api", None)):
            from .core.dashboard import Dashboard
            self.web = Dashboard(self.runtime, self.community._provider_options)
            self.web.register(context)

    def _c(self, key: str, default: Any = None) -> Any:
        value = self.config.get("publishing", {}).get(key, default)
        return default if value is None else value

    def _view_limit(self) -> int:
        """可见范围：1 公开、2 粉丝可见、3 仅自己可见。"""
        try:
            value = int(self._c("view_limit", 2))
        except (TypeError, ValueError):
            value = 2
        return value if value in (1, 2, 3) else 2

    def _cookie(self, profile_id: str = "") -> str:
        profile_id = self.publisher.profile_id(profile_id)
        credentials = self.runtime.credentials.load(profile_id)
        return "; ".join(f"{key}={value}" for key, value in credentials.cookies.items()) if credentials else ""

    def _get_client(self, profile_id: str = "") -> XhhClient:
        profile_id = self.publisher.profile_id(profile_id)
        credentials = self.publisher.credentials(profile_id)
        cookie = self._cookie(profile_id)
        if not cookie:
            raise XhhError("该档案缺少网页 Cookie，发帖请在统一账号面板重新扫码")
        client = self._clients.get(profile_id)
        if (client is None or cookie != client.cookie
                or credentials.uid != client.heybox_id):
            if client is not None:
                try:
                    asyncio.create_task(client.close())
                except Exception:
                    pass
            client = XhhClient(
                cookie, heybox_id=credentials.uid,
                timeout=float(self._c("request_timeout", 20)), outbox=self._get_outbox(),
            )
            self._clients[profile_id] = client
        self._client = client
        return client

    def _get_outbox(self) -> Outbox:
        if self._outbox is None:
            path = str(self._c("outbox_path", "") or "").strip()
            target = Path(path) if path else self._default_outbox_path()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            dedup = int(self._c("outbox_dedup_seconds", 600))
            self._outbox = Outbox(str(target), dedup_seconds=dedup)
        return self._outbox

    def _default_outbox_path(self) -> Path:
        """outbox 默认落在 AstrBot 插件数据目录，避免升级覆盖插件目录。"""
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME)) / "outbox.sqlite3"
        except Exception:
            pass
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "outbox.sqlite3"
        except Exception:
            return Path(__file__).resolve().parent / "outbox.sqlite3"

    async def terminate(self) -> None:
        await self.community.terminate()
        try:
            await self.runtime.close()
        finally:
            unbind_runtime(self.runtime)
            self.publisher.close()
            for client in set(self._clients.values()):
                await client.close()
            self._clients.clear()
            self._client = None
            if self._outbox is not None:
                self._outbox.close()
                self._outbox = None

    async def initialize(self) -> None:
        await self.runtime.ensure_started()
        await self.community.initialize()
        days = int(self._c("outbox_purge_days", 7))
        if days > 0:
            self._get_outbox().purge(status="failed", older_than_days=days)

    @filter.on_waiting_llm_request(priority=1000)
    async def prepare_xiaoheihe_before_agent(self, event: AstrMessageEvent):
        await self.community.prepare_xiaoheihe_before_agent(event)

    @filter.on_llm_request()
    async def inject_xiaoheihe_context(self, event: AstrMessageEvent, req: ProviderRequest):
        await self.community.inject_xiaoheihe_context(event, req)

    @filter.on_using_llm_tool(priority=-1000)
    async def isolate_xiaoheihe_images_for_grok(self, event: AstrMessageEvent, tool: object, tool_args: dict | None):
        await self.community.isolate_xiaoheihe_images_for_grok(event, tool, tool_args)

    @filter.on_llm_tool_respond(priority=1000)
    async def restore_xiaoheihe_images_after_grok(self, event: AstrMessageEvent, tool: object, tool_args: dict | None, tool_result: object | None):
        await self.community.restore_xiaoheihe_images_after_grok(event, tool, tool_args, tool_result)

    @filter.on_agent_done(priority=1000)
    async def restore_xiaoheihe_images_on_agent_done(self, event: AstrMessageEvent, run_context: object, response: LLMResponse):
        await self.community.restore_xiaoheihe_images_on_agent_done(event, run_context, response)

    @filter.on_llm_response(priority=-1000)
    async def capture_xiaoheihe_complete_reply(self, event: AstrMessageEvent, response: LLMResponse):
        await self.community.capture_xiaoheihe_complete_reply(event, response)

    @filter.on_agent_begin(priority=1000)
    async def mark_xiaoheihe_agent_started(self, event: AstrMessageEvent, run_context: object):
        await self.community.mark_xiaoheihe_agent_started(event, run_context)

    @filter.on_agent_done(priority=-1000)
    async def mark_xiaoheihe_agent_done(self, event: AstrMessageEvent, run_context: object, response: LLMResponse):
        await self.community.mark_xiaoheihe_agent_done(event, run_context, response)

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
            "heybox": "xhh", "xiaoheihe": "xhh", "heybox_community": "xhh",
        }.get(platform, platform)
        return f"{platform}:{sender}"

    def _lock_for(self, sender: str) -> asyncio.Lock:
        lock = self._locks.get(sender)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[sender] = lock
        return lock

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
                return "登录态失效，请在统一账号面板重新扫码。"
            return str(exc)
        if isinstance(exc, ValueError):
            return f"参数错误：{exc}"
        return f"操作失败（{type(exc).__name__}），请查看管理面板状态。"

    def _log_error(self, exc: Exception) -> None:
        """记录错误类型与接口，不输出可能含凭据的服务端响应。"""
        if isinstance(exc, XhhError):
            logger.warning(
                f"[{PLUGIN_NAME}] 发帖失败：{type(exc).__name__} "
                f"path={exc.path}"
            )

    @staticmethod
    def _command_args(event: AstrMessageEvent, command: str) -> str:
        raw = event.message_str.strip()
        for prefix in (f"/{command}", command):
            if raw.startswith(prefix):
                return raw[len(prefix):].strip()
        return raw

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
            "请登录 AstrBot WebUI → 插件 → 小黑盒社区 → 可视化视图 → 扫码登录，扫码登录或切换账号。"
        )

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
            yield event.plain_result("未登录或登录态已失效，请在统一账号面板扫码")

    @filter.command("xhh发帖")
    async def cmd_post(self, event: AstrMessageEvent):
        """手动发帖：/xhh发帖 标题|正文|话题名或ID"""
        if not self._is_allowed(event):
            yield event.plain_result("你没有发帖权限。请配置 AstrBot 管理员或插件 allowed_users 白名单。")
            return
        raw = self._command_args(event, "xhh发帖")
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            yield event.plain_result("用法：/xhh发帖 标题|正文|话题名或ID（第三段可省略，用默认话题）")
            return

        result = await self._publish(
            event, parts[0], parts[1], topic=parts[2] if len(parts) > 2 else ""
        )
        yield event.plain_result(result)

    @filter.command("xhh话题")
    async def cmd_topics(self, event: AstrMessageEvent):
        """搜话题/社区 ID：/xhh话题 Steam"""
        keyword = self._command_args(event, "xhh话题")
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
        link_id = self._command_args(event, "xhh删除")
        if not link_id.isascii() or not link_id.isdigit():
            yield event.plain_result("用法：/xhh删除 <link_id>")
            return
        if self.publisher.dry_run(self.publisher.profile_id()):
            yield event.plain_result(f"[dry-run] 未真正删除，link_id={link_id}")
            return
        try:
            result = await self._get_client().delete_link(link_id)
            yield event.plain_result(
                f"已删除 link_id={link_id}"
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
        profile_id: str = "",
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
            profile_id(string): 账号档案 ID，留空使用发布设置中的默认账号
        """
        if not self._is_allowed(event):
            return "当前用户没有发帖权限。请配置 AstrBot 管理员或插件 allowed_users 白名单。"
        return await self._publish(
            event, title, text, topic=topic, topic_ids=topic_ids, mode=mode, draft=draft, profile_id=profile_id
        )

    @filter.command("xhh评论")
    async def cmd_comment(self, event: AstrMessageEvent):
        """手动评论：/xhh评论 帖子ID|正文|父评论ID（可省略）"""
        parts = self._command_args(event, "xhh评论").split("|", 2)
        if len(parts) < 2:
            yield event.plain_result("用法：/xhh评论 帖子ID|正文|父评论ID（可省略）")
            return
        yield event.plain_result(await self.tool_comment(
            event, parts[0].strip(), parts[1].strip(),
            parent_comment_id=parts[2].strip() if len(parts) > 2 else "",
        ))

    @filter.llm_tool(name="xhh_publish_comment")
    async def tool_comment(self, event: AstrMessageEvent, post_id: str, text: str,
                           parent_comment_id: str = "", root_comment_id: str = "",
                           profile_id: str = "") -> str:
        """提交评论，经统一审核后发送。

        Args:
            post_id(string): 帖子 ID
            text(string): 评论正文
            parent_comment_id(string): 回复的父评论 ID，留空评论主帖
            root_comment_id(string): 根评论 ID，留空由服务端确定
            profile_id(string): 账号档案 ID，留空使用默认发布账号
        """
        if not self._is_allowed(event):
            return "当前用户没有评论权限。"
        try:
            profile_id = self.publisher.profile_id(profile_id)
            account_id = ""
            try:
                account_id = self.publisher.credentials(profile_id).uid
            except ValueError:
                if not self.publisher.dry_run(profile_id):
                    raise
            result = await self.publisher.submit("comment", profile_id, dict(
                post_id=post_id, text=text, parent_comment_id=parent_comment_id,
                root_comment_id=root_comment_id), source=self._sender(event),
                expected_account_id=account_id)
            return self._publish_result(result)
        except Exception as exc:
            self._log_error(exc)
            return self._friendly_error(exc)

    async def _publish(
        self, event: AstrMessageEvent, title: str, text: str, *,
        topic: str = "", topic_ids: str = "", mode: str = "dynamic", draft: bool = False,
        profile_id: str = "",
    ) -> str:
        """指令、工具和面板共用审核服务，权限在此再次校验。"""
        if not self._is_allowed(event):
            return "当前用户没有发帖权限。"
        try:
            profile_id = self.publisher.profile_id(profile_id)
            account_id = ""
            try:
                account_id = self.publisher.credentials(profile_id).uid
            except ValueError:
                if not self.publisher.dry_run(profile_id):
                    raise
        except Exception as exc:
            return self._friendly_error(exc)
        sender = self._sender(event)
        async with self._lock_for(sender):
            wait = self._check_cooldown(event)
            if wait:
                return wait
            try:
                ids = await self._resolve_topics(topic, topic_ids, profile_id=profile_id)
                if account_id and self.publisher.credentials(profile_id).uid != account_id:
                    return "账号已切换，原内容未提交，请重新发起。"
                result = await self.publisher.submit("post", profile_id, dict(
                    title=title, text=text, topic_ids=ids, mode=mode, draft=draft,
                    view_limit=self._view_limit()), source=sender,
                    expected_account_id=account_id)
                return self._publish_result(result)
            except Exception as exc:
                self._log_error(exc)
                return self._friendly_error(exc)

    @staticmethod
    def _publish_result(result: dict) -> str:
        status = result["status"]
        if status == "pending":
            return f"已提交审核（#{result['review']['id']}），请在管理面板确认内容和账号后批准。"
        if status == "dry_run":
            return "[dry-run] 已预览，未实际发送；审核项保留待处理。"
        if status == "sent":
            sent = result.get("result") or {}
            if sent.get("duplicate"):
                return f"相同内容已在去重窗口内发送过，link_id={sent.get('link_id', '')}。"
            label = "草稿已保存" if sent.get("is_draft") else "发布成功"
            return f"{label}，ID={sent.get('link_id') or sent.get('external_comment_id') or '未知'}。"
        if status in {"sending", "send_unknown"}:
            return "发送结果未知或正在发送，请在面板核实，勿重复提交。"
        return result.get("error") or f"该内容已处理，状态：{status}。"

    # ------------------------------------------------------------------
    async def _resolve_topics(self, topic: str = "", raw_ids: str = "", *, profile_id: str = "") -> list[str]:
        """话题解析优先级：显式 ID > 名称搜索 > 配置默认值。"""
        ids = self._normalize_ids(str(raw_ids or ""))
        if ids:
            return ids

        keyword = str(topic or "").strip()
        if keyword:
            if keyword.isdigit():
                return [keyword]
            found = await self._get_client(profile_id).search_topics(keyword)
            if not found:
                raise XhhError(f"没有找到话题「{keyword}」，请先用 xhh_search_topics 确认")
            return [found[0]["topic_id"]]

        ids = self._normalize_ids(str(self._c("default_topic_ids", "") or ""))
        if not ids:
            raise XhhError("缺少话题：请传 topic / topic_ids，或在插件配置里设置 default_topic_ids")
        return ids
