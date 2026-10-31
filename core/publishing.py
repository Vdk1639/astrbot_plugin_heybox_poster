"""发帖、评论共用的账号检查、审核及发送入口。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..xiaoheihe.api_client import SendUncertainError, XiaoheiheApiError
from ..xiaoheihe.models import EventState, RoutingTarget
from ..xiaoheihe.security import sanitize_reply_text
from .client import NetworkError, RateLimited, XhhError
from .post import build_post_form
from .review import ReviewQueue


class PublishingService:
    def __init__(self, runtime, client_factory, outbox_factory):
        self.runtime = runtime
        self.client_factory = client_factory
        self.outbox_factory = outbox_factory
        self.queue = ReviewQueue(
            str(runtime.data_dir / "reviews.sqlite3"),
            dedup_seconds=self.settings["outbox_dedup_seconds"],
        )

    @property
    def settings(self) -> dict:
        return self.runtime.config.snapshot()["publishing"]

    def profile_id(self, profile_id: str = "") -> str:
        if not isinstance(profile_id, str):
            raise ValueError("账号档案 ID 必须是字符串")
        value = profile_id or self.settings["profile_id"]
        profile = self.runtime.config.profile(value)
        if not profile.get("enabled", True):
            raise ValueError("所选账号档案已停用")
        return value

    def credentials(self, profile_id: str):
        self.profile_id(profile_id)
        credentials = self.runtime.credentials.load(profile_id)
        if credentials is None or not credentials.uid or not (
            credentials.cookies or credentials.access_token or credentials.refresh_token
        ):
            raise ValueError("所选账号尚未登录，请在账号面板扫码")
        return credentials

    def dry_run(self, profile_id: str, *, proactive: bool = False) -> bool:
        config = self.runtime.config.snapshot()
        return bool(
            config["publishing"]["dry_run"]
            or self.runtime.config.profile(profile_id).get("dry_run", True)
            or (proactive and config["proactive_feed"]["dry_run"])
        )

    def status(self, profile_id: str = "") -> dict:
        profile_id = self.profile_id(profile_id)
        credentials = self.runtime.credentials.load(profile_id)
        return {
            "profile_id": profile_id,
            "has_credentials": bool(credentials),
            "uid": credentials.uid if credentials else "",
            "dry_run": self.dry_run(profile_id),
            "post_review_required": self.settings["post_review_required"],
            "comment_review_required": self.settings["comment_review_required"],
            "counts": self.queue.counts(),
        }

    def normalize(self, kind: str, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是对象")
        if kind == "post":
            allowed = {"title", "text", "topic_ids", "mode", "draft", "view_limit"}
            if set(payload) - allowed:
                raise ValueError("帖子包含不支持的字段")
            title, text = payload.get("title", ""), payload.get("text", "")
            ids = payload.get("topic_ids", [])
            mode, draft = payload.get("mode", "dynamic"), payload.get("draft", False)
            visibility = payload.get("view_limit", self.settings["view_limit"])
            if not isinstance(title, str) or not isinstance(text, str):
                raise ValueError("标题和正文必须是字符串")
            if not isinstance(ids, list) or not all(isinstance(x, str) and x.isascii() and x.isdigit() for x in ids):
                raise ValueError("话题必须是数字 ID 列表")
            if mode not in {"dynamic", "article"} or type(draft) is not bool:
                raise ValueError("帖子类型或草稿标记无效")
            if type(visibility) is not int or visibility not in {1, 2, 3}:
                raise ValueError("可见范围只能是 1、2 或 3")
            build_post_form(title, text, mode=mode, topic_ids=ids, draft=draft, view_limit=visibility)
            return dict(title=title.strip(), text=text.strip(), topic_ids=ids,
                        mode=mode, draft=draft, view_limit=visibility)
        if kind != "comment":
            raise ValueError("发布类型只能是 post 或 comment")
        if set(payload) - {"text", "post_id", "parent_comment_id", "root_comment_id"}:
            raise ValueError("评论包含不支持的字段")
        result = {}
        for key in ("post_id", "parent_comment_id", "root_comment_id"):
            value = payload.get(key, "")
            if not isinstance(value, str) or (value and not (value.isascii() and value.isdigit())):
                raise ValueError("帖子和楼层 ID 必须是数字字符串")
            result[key] = value
        if not result["post_id"]:
            raise ValueError("缺少帖子 ID")
        if not isinstance(payload.get("text"), str):
            raise ValueError("评论正文必须是字符串")
        result["text"] = sanitize_reply_text(
            payload["text"], self.runtime.config.snapshot()["reply"]["max_reply_chars"],
        )
        return result

    async def submit(self, kind: str, profile_id: str, payload: dict, *,
                     source: str = "dashboard", source_id: str = "",
                     force_review: bool = False, metadata: dict | None = None,
                     expected_account_id: str = "") -> dict:
        profile_id = self.profile_id(profile_id)
        if expected_account_id and self.credentials(profile_id).uid != expected_account_id:
            raise ValueError("生成期间账号已切换，原内容不能提交")
        payload = self.normalize(kind, payload)
        metadata = metadata or {}
        simulated = self.dry_run(profile_id, proactive=metadata.get("proactive", False))
        simulated = simulated or metadata.get("dry_run", False)
        try:
            credentials = self.credentials(profile_id)
        except ValueError:
            if simulated:
                return {"status": "dry_run", "payload": payload}
            raise
        # 元数据来自原生事件入口，不能由面板请求任意指定。
        if metadata:
            payload["_delivery"] = metadata
        self.queue.dedup_seconds = self.settings["outbox_dedup_seconds"]
        row = self.queue.submit(kind, profile_id, credentials.uid, payload,
                                source=source, source_id=source_id)
        if row["status"] != "pending":
            return {"status": row["status"], "review": row, "result": row["result"]}
        if force_review or self.settings[f"{kind}_review_required"]:
            return {"status": "pending", "review": row}
        return await self.approve(row["id"])

    async def submit_comment(self, route: RoutingTarget, content: str, *,
                             event_id: int | None = None, dry_run: bool = False,
                             proactive: bool = False, generated_ms: int | None = None,
                             force_review: bool = False, expected_account_id: str = "") -> dict:
        if expected_account_id and self.credentials(route.profile_id).uid != expected_account_id:
            raise ValueError("生成回复期间账号已切换，原事件不能提交")
        config = self.runtime.config.snapshot()
        result = await self.submit(
            "comment", route.profile_id,
            dict(text=content, post_id=route.post_id,
                 parent_comment_id=route.parent_comment_id, root_comment_id=route.root_comment_id),
            source="proactive" if proactive else "notification",
            source_id=str(event_id) if event_id is not None else "",
            force_review=force_review or dry_run or (proactive and config["proactive_feed"]["review_required"]),
            metadata=dict(event_id=event_id, proactive=proactive,
                          generated_ms=generated_ms, notification_id=route.notification_id),
            expected_account_id=expected_account_id,
        )
        if event_id is not None and result["status"] == "pending":
            await self.runtime.repository.mark_event(
                event_id, EventState.GENERATED, reply_text=result["review"]["payload"]["text"],
                generated_ms=generated_ms,
            )
        if event_id is not None and result["status"] == "dry_run":
            await self.runtime.repository.mark_event(event_id, EventState.DRY_RUN)
        return result

    async def approve(self, review_id: int) -> dict:
        row = self.queue.get(review_id)
        if row is None or row["status"] != "pending":
            raise ValueError("审核项不存在或已处理")
        profile_id = self.profile_id(row["profile_id"])
        credentials = self.credentials(profile_id)
        if credentials.uid != row["account_id"]:
            raise ValueError("账号已切换，不能发送旧账号的审核内容")
        metadata = row["payload"].get("_delivery", {})
        if self.dry_run(profile_id, proactive=metadata.get("proactive", False)):
            return {"status": "dry_run", "review": row}
        payload = self.normalize(row["kind"], {k: v for k, v in row["payload"].items() if k != "_delivery"})
        cooldown_key = f"id:{credentials.uid}:publishing"
        outbox = self.outbox_factory()
        if outbox.cooldown_until(cooldown_key) > time.time():
            raise ValueError("该账号仍在发布冷却期，请稍后审核")
        # claim 前无 await；同一事件循环中的换号和第二次审批无法插入。
        client = self.client_factory(profile_id) if row["kind"] == "post" else None
        row = self.queue.claim(review_id, profile_id=profile_id, account_id=credentials.uid)
        seconds = self.settings["cooldown_seconds"]
        if seconds:
            outbox.set_cooldown(cooldown_key, time.time() + seconds)
        try:
            if row["kind"] == "post":
                current = self.credentials(profile_id)
                if current.uid != row["account_id"]:
                    raise ValueError("账号已切换，不能发送旧账号的审核内容")
                result = await client.publish(
                    payload["title"], payload["text"], topic_ids=payload["topic_ids"],
                    mode=payload["mode"], draft=payload["draft"], view_limit=payload["view_limit"],
                )
                # 原始上游响应不进入审核库或 Dashboard。
                result = {key: result.get(key) for key in ("link_id", "is_draft", "duplicate")}
                status = "sent" if result["link_id"] else "send_unknown"
            else:
                route = RoutingTarget(
                    profile_id=profile_id, post_id=payload["post_id"],
                    parent_comment_id=payload["parent_comment_id"], root_comment_id=payload["root_comment_id"],
                    notification_id=metadata.get("notification_id", ""),
                )
                await self.runtime.ensure_started()
                result = await self.runtime.deliver(
                    event_id=metadata.get("event_id"), route=route, content=payload["text"],
                    dry_run=False, proactive=metadata.get("proactive", False),
                    generated_ms=metadata.get("generated_ms"), reviewed=True,
                    expected_account_id=row["account_id"],
                )
                status = result["status"]
            row = self.queue.finish(review_id, status, result=result)
            return {"status": status, "review": row, "result": result}
        except BaseException as exc:
            uncertain = isinstance(exc, (NetworkError, SendUncertainError)) or not isinstance(
                exc, (XhhError, XiaoheiheApiError, ValueError),
            )
            status = "send_unknown" if uncertain else "failed"
            if isinstance(exc, RateLimited) or getattr(exc, "category", "") == "rate_limited":
                outbox.set_cooldown(cooldown_key, time.time() + self.settings["rate_limit_cooldown_seconds"])
            # 异常消息可能带服务器原文，面板只展示固定描述和类型。
            error = f"{'发送结果未知，请核实后处理' if uncertain else '发送被拒绝'} ({type(exc).__name__})"
            row = self.queue.finish(review_id, status, error=error)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not isinstance(exc, Exception):
                raise
            return {"status": status, "review": row, "error": error}

    async def reject(self, review_id: int) -> dict:
        row = self.queue.reject(review_id)
        event_id = row["payload"].get("_delivery", {}).get("event_id")
        if event_id is not None:
            await self.runtime.ensure_started()
            await self.runtime.repository.mark_event(event_id, EventState.IGNORED, error="审核拒绝")
        return row

    def close(self) -> None:
        self.queue.close()
