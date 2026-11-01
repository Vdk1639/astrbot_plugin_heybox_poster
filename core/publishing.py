"""发帖、评论共用的账号检查、审核及发送入口。"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from ..xiaoheihe.api_client import SendUncertainError, XiaoheiheApiError
from ..xiaoheihe.models import EventState, RoutingTarget
from ..xiaoheihe.permission_service import PermissionService
from ..xiaoheihe.policy import PolicyBlockedError
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

    async def _source_context(self, profile_id: str, event_id: int | None) -> dict:
        if event_id is None:
            return {}
        event = await self.runtime.repository.db.fetchone(
            "SELECT * FROM incoming_events WHERE id=? AND profile_id=?", (event_id, profile_id),
        )
        if event is None:
            return {}
        raw = json.loads(event["raw_json"] or "{}")
        return dict(event_id=event_id, profile_id=profile_id, sender_uid=event["sender_uid"],
                    event_type=event["event_type"], account_id=event["reply_account_id"],
                    **raw.get("_adapter_policy", {}))

    async def source_events(self, row: dict) -> list[dict]:
        sources = self.queue.sources(row["id"])
        metadata = row["payload"].get("_delivery", {})
        if row["source"] in {"notification", "proactive", "proactive_candidate"} and not any(
            source["source"] == row["source"] and source["source_id"] == row["source_id"] for source in sources
        ):
            sources.append(dict(source=row["source"], source_id=row["source_id"], source_context={}))
        if not sources and metadata.get("event_id") is not None:
            sources = [dict(source="proactive" if metadata.get("proactive") else "notification",
                            source_id=str(metadata["event_id"]), source_context={})]
        events = {}
        for source in sources:
            if source["source"] not in {"notification", "proactive", "proactive_candidate"}:
                continue
            source_id = source["source_id"]
            if source["source"] == "proactive_candidate" and source_id.startswith("candidate:"):
                candidate = await self.runtime.repository.db.fetchone(
                    "SELECT incoming_event_id FROM feed_candidates WHERE id=? AND profile_id=?",
                    (source_id.removeprefix("candidate:"), row["profile_id"]),
                )
                event_id = candidate["incoming_event_id"] if candidate else None
            else:
                event_id = int(source_id) if source_id.isascii() and source_id.isdigit() else None
            event = await self.runtime.repository.db.fetchone(
                "SELECT * FROM incoming_events WHERE id=? AND profile_id=?", (event_id, row["profile_id"]),
            ) if event_id is not None else None
            snapshot = source.get("source_context", {})
            data = dict(event) if event is not None else dict(snapshot)
            valid = (
                source.get("kind", "comment") == "comment"
                and source.get("profile_id", row["profile_id"]) == row["profile_id"]
                and source.get("account_id", row["account_id"]) == row["account_id"]
                and event_id is not None
                and data.get("profile_id") == row["profile_id"]
                and (not data.get("reply_account_id") or data["reply_account_id"] == row["account_id"])
                and (not snapshot or (snapshot.get("event_id") == event_id
                     and snapshot.get("profile_id") == row["profile_id"]
                     and snapshot.get("account_id", row["account_id"]) in ("", row["account_id"])
                     and snapshot.get("sender_uid") == data.get("sender_uid")))
            )
            if source["source"] == "notification":
                valid = valid and data.get("event_type") in {"mention", "reply"}
            else:
                valid = valid and data.get("event_type") == "proactive_feed"
            data.update(id=event_id, source=source["source"], identity_available=valid)
            if event is not None:
                raw = json.loads(event["raw_json"] or "{}")
                for key in ("post_author_uid", "explicit_wake"):
                    if key in snapshot:
                        data[key] = snapshot[key]
                    if key in raw.get("_adapter_policy", {}):
                        data[key] = raw["_adapter_policy"][key]
            events[(source["source"], source_id)] = data
        return list(events.values())

    async def check_comment_policy(self, row: dict) -> None:
        config = self.runtime.config.snapshot()
        profile = self.runtime.config.profile(row["profile_id"])
        checked_sources = self.queue.sources(row["id"])
        for event in await self.source_events(row):
            if event["source"] != "notification":
                continue
            if not event["identity_available"] or event.get("profile_id") != row["profile_id"]:
                raise PolicyBlockedError("risk_source_unresolved", "通知来源身份无法核实，未发送，仍待审核")
            uid = str(event.get("sender_uid") or "").strip()
            if uid:
                risk = await self.runtime.repository.risk_user(
                    row["profile_id"], uid, risk_config=config["risk"],
                    owner_uid=profile.get("owner_uid", ""),
                )
                self.runtime.record_user_filter(row["profile_id"], uid, risk["reason"])
                if risk["reason"]:
                    message = "该通知用户已被人工停用" if risk["manual_blocked"] else "该通知用户当前达到自动过滤阈值"
                    raise PolicyBlockedError(risk["reason"], message)
            if config["reply"]["only_explicit_mentions"] and "explicit_wake" not in event:
                raise PolicyBlockedError("risk_source_unresolved", "通知触发方式无法核实，未发送，仍待审核")
            permissions = PermissionService(
                config["permissions"], owner_uid=profile.get("owner_uid", ""), self_uid=row["account_id"],
                only_explicit_mentions=config["reply"]["only_explicit_mentions"],
                reply_to_direct_replies=config["reply"]["reply_to_direct_replies"],
            )
            if config["permissions"]["keyword_blacklist"] and event.get("content") is None:
                raise PolicyBlockedError("risk_source_unresolved", "通知正文已清理，无法复查当前关键词策略")
            notification = SimpleNamespace(
                sender_uid=uid, sender_uid_verified=bool(uid), post_author_uid=event.get("post_author_uid", ""),
                content=event.get("content") or "", explicit_wake=event.get("explicit_wake", True),
                event_type=SimpleNamespace(value=event.get("event_type", "reply")),
            )
            decision = permissions.decide(notification)
            if not decision.allowed:
                raise PolicyBlockedError("reply_policy_changed", decision.reason)
        if self.queue.sources(row["id"]) != checked_sources:
            raise PolicyBlockedError("reply_policy_changed", "校验期间审核来源已更新，请重新审核")

    async def settle_sources(self, row: dict) -> None:
        outcome = row["status"]
        state = {"pending": "generated", "sending": "generated", "rejected": "ignored", "failed": "dead_letter"}.get(outcome, outcome)
        completed_at = row["updated_at"] if outcome in {"sent", "rejected", "failed"} else None
        for event in await self.source_events(row):
            event_id = event.get("id")
            if event_id is None or not event["identity_available"]:
                self.runtime._alerts[f"review_source:{row['id']}"] = dict(
                    key=f"review_source:{row['id']}", level="warning",
                    message=f"审核 {row['id']} 的来源关联无法核实，已保留原记录等待核对",
                )
                continue
            if event["source"] == "notification":
                await self.runtime.repository.restore_reply_admission(
                    event_id, row["account_id"], outcome,
                    dedup_days=self.runtime.config.snapshot()["retention"]["dedup_days"],
                    completed_at=completed_at,
                )
                await self.runtime.repository.settle_reply_admission(
                    event_id, outcome, release=outcome in {"rejected", "failed"},
                )
            await self.runtime.repository.db.execute(
                "UPDATE incoming_events SET status=?, error=?, should_filter=?, updated_at=?, completed_at=COALESCE(completed_at,?) "
                "WHERE id=? AND (status NOT IN ('sent','send_unknown','ignored','dry_run','dead_letter') "
                "OR status=? OR (status='send_unknown' AND ?='sent'))",
                (state, "审核拒绝" if outcome == "rejected" else row.get("error", ""),
                 int(outcome == "rejected"), time.time(), completed_at, event_id, state, state),
            )

    async def reconcile(self) -> None:
        repo = self.runtime.repository
        await repo.db.execute("UPDATE outgoing_replies SET status='send_unknown' WHERE status='sending'")
        for row in self.queue.all_comments():
            # Old attempts can only inherit the UID bound by their review.
            for event in await self.source_events(row):
                if event.get("id") is not None and event["identity_available"]:
                    await repo.db.execute(
                        "UPDATE outgoing_replies SET account_id=?, send_key=?, attempt_no=id "
                        "WHERE incoming_event_id=? AND account_id=''",
                        (row["account_id"], f"review:{row['id']}", event["id"]),
                    )
            outgoing = await repo.db.fetchone(
                "SELECT * FROM outgoing_replies WHERE send_key=? AND account_id=? ORDER BY id DESC LIMIT 1",
                (f"review:{row['id']}", row["account_id"]),
            )
            if outgoing and outgoing["status"] == "sent" and row["status"] == "send_unknown":
                row = self.queue.confirm_recovered(row["id"], dict(status="sent", external_comment_id=outgoing["external_comment_id"]))
            if row["status"] == "send_unknown" and outgoing is None:
                self.runtime._alerts[f"review_attempt:{row['id']}"] = dict(
                    key=f"review_attempt:{row['id']}", level="warning",
                    message=f"审核 {row['id']} 结果未知且缺少发送明细，已保留占位，请核对",
                )
            await self.settle_sources(row)
        if await repo.db.fetchone("SELECT id FROM outgoing_replies WHERE account_id='' AND status IN ('sent','sending','send_unknown') LIMIT 1"):
            self.runtime._alerts["legacy_comment_budget"] = dict(
                key="legacy_comment_budget", level="warning",
                message="部分历史评论缺少可信账号绑定，升级首日额度统计可能不完整；请核对后谨慎启用真实发送",
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
        source_context = await self._source_context(profile_id, metadata.get("event_id"))
        if self.credentials(profile_id).uid != credentials.uid:
            raise ValueError("账号已切换，原内容不能提交")
        self.queue.dedup_seconds = self.settings["outbox_dedup_seconds"]
        row = self.queue.submit(kind, profile_id, credentials.uid, payload,
                                source=source, source_id=source_id, source_context=source_context)
        if kind == "comment":
            await self.settle_sources(row)
        if row["status"] != "pending":
            return {"status": row["status"], "review": row, "result": row["result"]}
        if force_review or self.settings[f"{kind}_review_required"]:
            return {"status": "pending", "review": row}
        return await self.approve(row["id"])

    async def submit_comment(self, route: RoutingTarget, content: str, *,
                             event_id: int | None = None, dry_run: bool = False,
                             proactive: bool = False, generated_ms: int | None = None,
                             force_review: bool = False, expected_account_id: str = "") -> dict:
        if event_id is not None:
            bound = await self.runtime.repository.admission_account_id(event_id)
            if bound:
                if expected_account_id and bound != expected_account_id:
                    raise ValueError("回复账号与通知准入不匹配")
                expected_account_id = bound
            event = await self.runtime.repository.db.fetchone(
                "SELECT status FROM incoming_events WHERE id=?", (event_id,),
            )
            if event and event["status"] in {"ignored", "dead_letter", "dry_run"} and not self.queue.has_event(route.profile_id, event_id):
                raise ValueError("原事件已经结束，已抑制迟到回复")
        if expected_account_id and self.credentials(route.profile_id).uid != expected_account_id:
            raise ValueError("生成回复期间账号已切换，原事件不能提交")
        config = self.runtime.config.snapshot()
        result = await self.submit(
            "comment", route.profile_id,
            dict(text=content, post_id=route.post_id,
                 parent_comment_id=route.parent_comment_id, root_comment_id=route.root_comment_id),
            source=("proactive" if proactive else "notification") if event_id is not None else "session",
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
        if row["kind"] == "comment":
            await self.runtime.ensure_started()
            try:
                await self.check_comment_policy(row)
            except PolicyBlockedError as exc:
                return self._policy_result(row, exc)
            credentials = self.credentials(profile_id)
            if credentials.uid != row["account_id"]:
                raise ValueError("账号已切换，不能发送旧账号的审核内容")
            if self.dry_run(profile_id, proactive=metadata.get("proactive", False)):
                return {"status": "dry_run", "review": row}
        payload = self.normalize(row["kind"], {k: v for k, v in row["payload"].items() if k != "_delivery"})
        cooldown_key = f"id:{credentials.uid}:publishing"
        outbox = self.outbox_factory()
        if outbox.cooldown_until(cooldown_key) > time.time():
            raise ValueError("该账号仍在发布冷却期，请稍后审核")
        # All awaits precede this last UID check and atomic claim.
        client = self.client_factory(profile_id) if row["kind"] == "post" else None
        row = self.queue.claim(review_id, profile_id=profile_id, account_id=credentials.uid)
        claim_token = row["claim_token"]
        seconds = self.settings["cooldown_seconds"]
        claimed_cooldown = time.time() + seconds if seconds else 0
        if seconds:
            outbox.set_cooldown(cooldown_key, claimed_cooldown)
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
                    send_key=f"review:{review_id}", review_id=review_id,
                )
                status = result["status"]
            row = self.queue.finish(review_id, status, result=result)
            await self.settle_sources(row)
            return {"status": status, "review": row, "result": result}
        except PolicyBlockedError as exc:
            # The runtime raises this only after proving no HTTP write began.
            row = self.queue.release_claim(review_id, claim_token, str(exc))
            if claimed_cooldown:
                outbox.release_cooldown(cooldown_key, claimed_cooldown)
            await self.settle_sources(row)
            return self._policy_result(row, exc)
        except BaseException as exc:
            if self.queue.get(review_id)["status"] != "sending":
                raise
            outgoing = await self.runtime.repository.db.fetchone(
                "SELECT status,error FROM outgoing_replies WHERE send_key=? AND account_id=? ORDER BY id DESC LIMIT 1",
                (f"review:{review_id}", row["account_id"]),
            ) if row["kind"] == "comment" else None
            if row["kind"] == "comment" and isinstance(exc, asyncio.CancelledError) and (
                outgoing is None or (outgoing["status"] == "retry_wait" and outgoing["error"] == "本地校验取消，未发送")
            ):
                row = self.queue.release_claim(review_id, claim_token, "本地校验取消，未发送")
                if claimed_cooldown:
                    outbox.release_cooldown(cooldown_key, claimed_cooldown)
                await self.settle_sources(row)
                raise
            uncertain = isinstance(exc, (NetworkError, SendUncertainError)) or not isinstance(
                exc, (XhhError, XiaoheiheApiError, ValueError),
            )
            if outgoing and outgoing["status"] == "failed":
                uncertain = False
            status = "send_unknown" if uncertain else "failed"
            if isinstance(exc, RateLimited) or getattr(exc, "category", "") == "rate_limited":
                outbox.set_cooldown(cooldown_key, time.time() + self.settings["rate_limit_cooldown_seconds"])
            # 异常消息可能带服务器原文，面板只展示固定描述和类型。
            error = f"{'发送结果未知，请核实后处理' if uncertain else '发送被拒绝'} ({type(exc).__name__})"
            row = self.queue.finish(review_id, status, error=error)
            await self.settle_sources(row)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not isinstance(exc, Exception):
                raise
            return {"status": status, "review": row, "error": error}

    async def reject(self, review_id: int) -> dict:
        row = self.queue.reject(review_id)
        await self.runtime.ensure_started()
        await self.settle_sources(row)
        return row

    @staticmethod
    def _policy_result(row: dict, exc: PolicyBlockedError) -> dict:
        return dict(status="pending", policy_blocked=True, reason=exc.reason, message=str(exc), review=row)

    def close(self) -> None:
        self.queue.close()
