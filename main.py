"""AstrBot 小黑盒发帖插件。

给 LLM 提供三个函数工具（发帖 / 搜话题 / 查登录态），另带几个手动指令。
签名、表单编码、风控错误处理都在 ``core`` 里，这里只做 AstrBot 适配。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.client import CaptchaRequired, XhhClient, XhhError, build_post_form

PLUGIN_NAME = "astrbot_plugin_heybox_poster"


@register(
    PLUGIN_NAME,
    "dsh",
    "小黑盒发帖：LLM 工具 + 手动指令，支持动态/长文/草稿",
    "0.1.0",
)
class XhhPosterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._client: XhhClient | None = None
        self._client_cookie = ""
        self._last_post_at: dict[str, float] = {}

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
        if not cookie:
            raise XhhError("还没有配置 cookie：请在插件配置里填 cookie（或 user_pkey + x_xhh_tokenid）")
        if self._client is None or cookie != self._client_cookie:
            if self._client is not None:
                # 旧客户端交给事件循环回收，这里不阻塞
                try:
                    import asyncio

                    asyncio.create_task(self._client.close())
                except Exception:
                    pass
            self._client = XhhClient(
                cookie,
                heybox_id=str(self._c("heybox_id", "") or ""),
                timeout=float(self._c("request_timeout", 20) or 20),
            )
            self._client_cookie = cookie
        return self._client

    async def terminate(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

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

    # ------------------------------------------------------------------
    # 权限 / 频控
    # ------------------------------------------------------------------
    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        allow = self._c("allowed_users", []) or []
        if isinstance(allow, str):
            allow = [x.strip() for x in allow.replace("，", ",").split(",")]
        allow = [str(x).strip() for x in allow if str(x).strip()]
        if not allow:
            return True
        try:
            sender = str(event.get_sender_id())
        except Exception:
            return False
        return sender in allow

    def _check_cooldown(self, event: AstrMessageEvent) -> str:
        seconds = int(self._c("cooldown_seconds", 60) or 0)
        if seconds <= 0:
            return ""
        try:
            sender = str(event.get_sender_id())
        except Exception:
            sender = "unknown"
        now = time.time()
        last = self._last_post_at.get(sender, 0)
        if now - last < seconds:
            wait = int(seconds - (now - last))
            return f"发帖太频繁了，请 {wait} 秒后再试。"
        self._last_post_at[sender] = now
        return ""

    # ------------------------------------------------------------------
    # 手动指令
    # ------------------------------------------------------------------
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
            yield event.plain_result("你没有发帖权限。")
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
        wait = self._check_cooldown(event)
        if wait:
            yield event.plain_result(wait)
            return

        try:
            topic_ids = await self._resolve_topics(topic)
            view_limit = self._view_limit()
            if self._c("dry_run", False):
                form = build_post_form(
                    title, text, topic_ids=topic_ids, view_limit=view_limit
                )
                yield event.plain_result(f"[dry-run] 将提交：{form}")
                return
            result = await self._get_client().publish(
                title,
                text,
                mode="dynamic",
                topic_ids=topic_ids,
                view_limit=view_limit,
            )
            yield event.plain_result(
                f"发布成功，link_id={result.get('link_id') or '（未返回）'}"
            )
        except CaptchaRequired as exc:
            yield event.plain_result(f"被风控拦了：{exc}。请用 xiaoheihe-cli 重新登录或更新 x_xhh_tokenid。")
        except Exception as exc:
            yield event.plain_result(f"发帖失败：{type(exc).__name__}: {exc}")

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
        lines = [f"{t['topic_id']}  {t['name']}" for t in topics[:10]]
        yield event.plain_result("话题 ID / 名称：\n" + "\n".join(lines))

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
        """按关键词搜索小黑盒社区/话题，发帖前用它确认 topic_id。

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
            f"- {t['name']}（topic_id={t['topic_id']}）" for t in topics[:10]
        )

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

        调用前请确认标题、正文已写好；话题必填，可用 xhh_search_topics 查 ID。

        Args:
            title(string): 标题，不超过 100 字
            text(string): 正文纯文本；mode=article 时可传简单 HTML
            topic(string): 社区/话题名称，例如 "Steam"，会现场搜索取第一个匹配
            topic_ids(string): 已知的话题 ID，多个用英文逗号分隔；与 topic 二选一即可
            mode(string): dynamic（动态，默认）或 article（长文）
            draft(boolean): 为 true 时只存草稿，不公开发布
        """
        if not self._is_allowed(event):
            return "当前用户没有发帖权限。"
        wait = self._check_cooldown(event)
        if wait:
            return wait
        try:
            ids = await self._resolve_topics(topic, topic_ids)
            view_limit = self._view_limit()
            if self._c("dry_run", False):
                form = build_post_form(
                    title,
                    text,
                    mode=mode,
                    topic_ids=ids,
                    draft=draft,
                    view_limit=view_limit,
                )
                return f"[dry-run] 未真正提交，表单如下：{form}"
            result = await self._get_client().publish(
                title,
                text,
                mode=mode,
                topic_ids=ids,
                draft=draft,
                view_limit=view_limit,
            )
        except CaptchaRequired as exc:
            return f"被风控验证码拦截（{exc}），请更新 cookie 里的 x_xhh_tokenid 后重试。"
        except Exception as exc:
            return f"发帖失败：{type(exc).__name__}: {exc}"

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
