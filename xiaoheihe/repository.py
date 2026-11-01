from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .database import Database
from .models import EventState, Notification, RoutingTarget
from .security import redact_data, redact_text
from .risk_service import RULE_VERSION, content_facts, risk_snapshot, text_hits

FINAL_STATES = {
    EventState.SENT.value,
    EventState.DRY_RUN.value,
    EventState.IGNORED.value,
    EventState.DEAD_LETTER.value,
}
MAX_CLEANUP_BATCHES_PER_RULE = 20


class Repository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def claim_event(self, notification: Notification, *, risk_config: dict[str, Any] | None = None,
                          owner_uid: str = '', self_uid: str = '') -> int | None:
        now = time.time()
        observed_at = notification.observed_at or now
        raw = dict(notification.raw)
        raw["_adapter_timing"] = {
            "source_created_at": notification.created_at,
            "observed_at": observed_at,
            "source_time_provided": notification.source_time_provided,
            "is_backfill": notification.is_backfill,
        }
        raw["_adapter_identity"] = {
            "sender_uid_verified": notification.sender_uid_verified,
            "sender_identity_key": notification.sender_identity_key,
        }
        raw["_adapter_policy"] = {
            "post_author_uid": notification.post_author_uid,
            "explicit_wake": notification.explicit_wake,
        }
        raw_json = json.dumps(
            redact_data(raw),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """
                INSERT OR IGNORE INTO incoming_events(
                    profile_id, external_event_id, external_comment_id,
                    notification_id, event_type, status, sender_uid,
                    sender_nickname, post_id, root_comment_id, parent_comment_id,
                    content, raw_json, discovered_at, claimed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification.profile_id,
                    notification.external_event_id,
                    notification.external_comment_id,
                    notification.notification_id,
                    notification.event_type.value,
                    EventState.CLAIMED.value,
                    str(notification.sender_uid or '').strip(),
                    notification.sender_nickname,
                    notification.post_id,
                    notification.root_comment_id,
                    notification.parent_comment_id,
                    notification.content,
                    raw_json,
                    observed_at,
                    now,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return None
            event_id = int(cursor.lastrowid)
            config = risk_config or {}
            uid = str(notification.sender_uid or '').strip()
            state = 'pending'
            if not config.get('enabled', True):
                state = 'skipped_disabled'
            elif notification.event_type.value not in {'mention', 'reply'}:
                state = 'skipped_source'
            elif not uid:
                state = 'skipped_uid'
            elif self_uid and uid == self_uid:
                state = 'skipped_self'
            elif owner_uid and uid == owner_uid and config.get('owner_exempt', True):
                state = 'skipped_owner'
            digest, text_eligible = content_facts(notification.content)
            source = notification.created_at
            window = config.get('flood_window_minutes', 10) * 60
            behavior = (notification.source_time_provided and not notification.is_backfill
                        and isinstance(source, (int, float)) and math.isfinite(source) and source > 0
                        and observed_at - window <= source <= observed_at
                        and notification.event_type.value in {'mention', 'reply'} and bool(uid))
            await connection.execute(
                'UPDATE incoming_events SET risk_state=?, risk_content_hash=?, risk_text_eligible=?, risk_behavior_eligible=? WHERE id=?',
                (state, digest, int(text_eligible), int(behavior), event_id),
            )
            await connection.execute(
                """
                INSERT OR IGNORE INTO processed_event_keys(
                    profile_id, event_key, key_type, created_at
                ) VALUES (?, ?, 'notification', ?)
                """,
                (notification.profile_id, notification.external_event_id, now),
            )
            return event_id

    async def claim_retry_event(self, notification: Notification) -> int | None:
        now = time.time()
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE incoming_events
                SET status = 'claimed', claimed_at = ?, updated_at = ?, error = ''
                WHERE profile_id = ? AND external_event_id = ?
                  AND status = 'retry_wait'
                  AND COALESCE(next_retry_at, 0) <= ?
                """,
                (
                    now,
                    now,
                    notification.profile_id,
                    notification.external_event_id,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = await connection.execute(
                """
                SELECT id FROM incoming_events
                WHERE profile_id = ? AND external_event_id = ?
                """,
                (notification.profile_id, notification.external_event_id),
            )
            result = await row.fetchone()
            return int(result["id"]) if result else None

    async def is_event_queueable(self, profile_id: str, external_event_id: str) -> bool:
        row = await self.db.fetchone(
            """
            SELECT status, next_retry_at
            FROM incoming_events
            WHERE profile_id = ? AND external_event_id = ?
            """,
            (profile_id, external_event_id),
        )
        if row is None:
            return True
        return (
            str(row["status"]) == EventState.RETRY_WAIT.value
            and float(row["next_retry_at"] or 0) <= time.time()
        )

    async def event_exists(self, profile_id: str, external_event_id: str) -> bool:
        row = await self.db.fetchone(
            """
            SELECT 1 FROM incoming_events
            WHERE profile_id = ? AND external_event_id = ?
            """,
            (profile_id, external_event_id),
        )
        return row is not None

    async def notification_cursor(
        self,
        profile_id: str,
        event_type: str,
    ) -> str | None:
        row = await self.db.fetchone(
            """
            SELECT last_event_id FROM notification_cursors
            WHERE profile_id = ? AND event_type = ?
            """,
            (profile_id, event_type),
        )
        return str(row["last_event_id"]) if row is not None else None

    async def initialize_notification_cursor(
        self,
        profile_id: str,
        event_type: str,
        last_event_id: str,
    ) -> bool:
        now = time.time()
        cursor = await self.db.execute(
            """
            INSERT OR IGNORE INTO notification_cursors(
                profile_id, event_type, last_event_id, initialized_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (profile_id, event_type, str(last_event_id), now, now),
        )
        return cursor.rowcount == 1

    async def advance_notification_cursor(
        self,
        profile_id: str,
        event_type: str,
        previous_event_id: str,
        last_event_id: str,
    ) -> bool:
        cursor = await self.db.execute(
            """
            UPDATE notification_cursors
            SET last_event_id = ?, updated_at = ?
            WHERE profile_id = ? AND event_type = ? AND last_event_id = ?
            """,
            (
                str(last_event_id),
                time.time(),
                profile_id,
                event_type,
                str(previous_event_id),
            ),
        )
        return cursor.rowcount == 1

    async def notification_backfill(
        self,
        profile_id: str,
        event_type: str,
    ) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT boundary_event_id, next_offset, started_at, updated_at
            FROM notification_backfills
            WHERE profile_id = ? AND event_type = ?
            """,
            (profile_id, event_type),
        )
        return dict(row) if row is not None else None

    async def advance_notification_cursor_with_backfill(
        self,
        profile_id: str,
        event_type: str,
        previous_event_id: str,
        last_event_id: str,
        *,
        next_offset: int,
    ) -> bool:
        """Advance the live cursor while durably retaining an older scan gap."""

        now = time.time()
        safe_offset = max(0, int(next_offset))
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE notification_cursors
                SET last_event_id = ?, updated_at = ?
                WHERE profile_id = ? AND event_type = ? AND last_event_id = ?
                """,
                (
                    str(last_event_id),
                    now,
                    profile_id,
                    event_type,
                    str(previous_event_id),
                ),
            )
            if cursor.rowcount != 1:
                return False
            existing_cursor = await connection.execute(
                """
                SELECT boundary_event_id, next_offset
                FROM notification_backfills
                WHERE profile_id = ? AND event_type = ?
                """,
                (profile_id, event_type),
            )
            existing = await existing_cursor.fetchone()
            if existing is None:
                await connection.execute(
                    """
                    INSERT INTO notification_backfills(
                        profile_id, event_type, boundary_event_id,
                        next_offset, started_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_id,
                        event_type,
                        str(previous_event_id),
                        safe_offset,
                        now,
                        now,
                    ),
                )
            else:
                # A newer burst can open another gap in front of an older one.
                # Keep the oldest boundary and rewind the offset so the two gaps
                # are drained as one continuous range instead of dropping the
                # newly created middle section.
                await connection.execute(
                    """
                    UPDATE notification_backfills
                    SET next_offset = MIN(next_offset, ?), updated_at = ?
                    WHERE profile_id = ? AND event_type = ?
                    """,
                    (safe_offset, now, profile_id, event_type),
                )
        return True

    async def advance_notification_backfill(
        self,
        profile_id: str,
        event_type: str,
        boundary_event_id: str,
        *,
        next_offset: int,
    ) -> bool:
        cursor = await self.db.execute(
            """
            UPDATE notification_backfills
            SET next_offset = ?, updated_at = ?
            WHERE profile_id = ? AND event_type = ? AND boundary_event_id = ?
            """,
            (
                max(0, int(next_offset)),
                time.time(),
                profile_id,
                event_type,
                str(boundary_event_id),
            ),
        )
        return cursor.rowcount == 1

    async def clear_notification_backfill(
        self,
        profile_id: str,
        event_type: str,
        boundary_event_id: str,
    ) -> bool:
        cursor = await self.db.execute(
            """
            DELETE FROM notification_backfills
            WHERE profile_id = ? AND event_type = ? AND boundary_event_id = ?
            """,
            (profile_id, event_type, str(boundary_event_id)),
        )
        return cursor.rowcount == 1

    async def reserve_reply_admission(
        self, event_id: int, notification: Notification, *, account_id: str,
        thread_once: bool = True, user_daily_limit: int = 5,
        account_daily_limit: int = 30, owner_exempt: bool = False,
        dedup_days: int = 365,
    ) -> dict[str, Any]:
        """Charge and claim one generation atomically; duplicates never execute."""
        if not isinstance(account_id, str) or not account_id.strip() or account_id.startswith('dry-run:'):
            raise ValueError("缺少回复账号 UID")
        for limit in (user_daily_limit, account_daily_limit, dedup_days):
            if type(limit) is not int or limit < 0:
                raise ValueError("通知处理上限必须是非负整数")
        now = time.time()
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        scope = json.dumps([notification.post_id, notification.root_comment_id or None], separators=(",", ":"))
        sender = f"uid:{notification.sender_uid.strip()}" if notification.sender_uid_verified else "unverified"
        async with self.db.transaction() as connection:
            cursor = await connection.execute("SELECT * FROM incoming_events WHERE id = ?", (event_id,))
            event = await cursor.fetchone()
            if event is None or (event["profile_id"], event["external_event_id"], event["post_id"],
                                 event["root_comment_id"], str(event["sender_uid"] or '').strip()) != (
                notification.profile_id, notification.external_event_id, notification.post_id,
                notification.root_comment_id, str(notification.sender_uid or '').strip(),
            ):
                raise ValueError("通知准入事件归属不一致")
            if event["reply_account_id"] and event["reply_account_id"] != account_id:
                return {"allowed": False, "created": False, "reason": "reply_account_changed"}
            cursor = await connection.execute(
                "SELECT * FROM reply_admissions WHERE incoming_event_id = ? ORDER BY attempt_no DESC LIMIT 1",
                (event_id,),
            )
            previous = await cursor.fetchone()
            if previous is not None and previous["outcome"] != "retry_wait":
                return {**dict(previous), "allowed": True, "created": False}
            if event["status"] != "claimed":
                return {"allowed": True, "created": False, "status": event["status"]}
            cursor = await connection.execute(
                "SELECT status FROM outgoing_replies WHERE incoming_event_id = ? ORDER BY id DESC LIMIT 1",
                (event_id,),
            )
            outgoing = await cursor.fetchone()
            if outgoing and outgoing["status"] not in {"retry_wait", "dry_run"}:
                return {"allowed": True, "created": False, "status": outgoing["status"]}
            if thread_once:
                cursor = await connection.execute(
                    "SELECT 1 FROM reply_admissions WHERE account_id = ? AND scope_key = ? "
                    "AND (floor_active = 1 OR (charged = 0 AND outcome IN ('pending','sending','sent','send_unknown'))) "
                    "AND incoming_event_id != ?",
                    (account_id, scope, event_id),
                )
                if await cursor.fetchone():
                    return {"allowed": False, "created": False, "reason": "reply_scope_taken"}
                # Old unbound rows are not evidence that the current UID sent them.
                cursor = await connection.execute(
                    "SELECT 1 FROM incoming_events e WHERE e.id != ? AND e.post_id = ? "
                    "AND e.root_comment_id = ? AND e.event_type IN ('mention', 'reply') "
                    "AND ((e.reply_account_id = ?) OR (e.reply_account_id = '' AND e.profile_id = ?)) "
                    "AND e.status IN ('generated', 'dispatched', 'sent', 'send_unknown') "
                    "AND (e.status != 'sent' OR e.completed_at IS NULL OR e.completed_at <= 0 OR e.completed_at >= ?) "
                    "AND NOT EXISTS (SELECT 1 FROM reply_admissions a WHERE a.incoming_event_id = e.id)",
                    (event_id, notification.post_id, notification.root_comment_id, account_id,
                     notification.profile_id, now-dedup_days*86400),
                )
                if await cursor.fetchone():
                    return {"allowed": False, "created": False, "reason": "reply_scope_taken"}
            cursor = await connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(sender_key = ?), 0) AS per_user "
                "FROM reply_admissions WHERE account_id = ? AND day = ? AND charged = 1",
                (sender, account_id, day),
            )
            counts = await cursor.fetchone()
            if user_daily_limit and not owner_exempt and counts["per_user"] >= user_daily_limit:
                return {"allowed": False, "created": False, "reason": "reply_user_daily_limit"}
            if account_daily_limit and counts["total"] >= account_daily_limit:
                return {"allowed": False, "created": False, "reason": "reply_account_daily_limit"}
            if previous is not None:
                await connection.execute("UPDATE reply_admissions SET floor_active = 0 WHERE id = ?", (previous["id"],))
            await connection.execute("UPDATE incoming_events SET reply_account_id = ? WHERE id = ?", (account_id, event_id))
            cursor = await connection.execute(
                "INSERT INTO reply_admissions(incoming_event_id, attempt_no, profile_id, account_id, "
                "sender_key, day, admitted_at, post_id, scope_key, floor_active, outcome, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing', ?)",
                (event_id, previous["attempt_no"] + 1 if previous else 1, notification.profile_id,
                 account_id, sender, day, now, notification.post_id, scope, int(thread_once), now),
            )
            return {"allowed": True, "created": True, "id": int(cursor.lastrowid),
                    "account_id": account_id, "outcome": "processing"}

    async def admission_account_id(self, event_id: int) -> str:
        row = await self.db.fetchone("SELECT reply_account_id FROM incoming_events WHERE id = ?", (event_id,))
        return str(row["reply_account_id"]) if row else ""

    async def _settle_reply_admission(
        self, connection, event_id: int, outcome: str, *, release: bool = False,
        admission_id: int | None = None,
    ) -> None:
        cursor = await connection.execute(
            "SELECT * FROM reply_admissions WHERE incoming_event_id = ? ORDER BY attempt_no DESC LIMIT 1",
            (event_id,),
        )
        row = await cursor.fetchone()
        if row is None or (admission_id is not None and row["id"] != admission_id):
            return
        if row["outcome"] in {"sent", "send_unknown", "execution_unknown"} and outcome not in {row["outcome"], "sent"}:
            return
        if row["outcome"] in {"rejected", "failed", "ignored", "dead_letter", "dry_run"} and outcome != row["outcome"]:
            return
        await connection.execute(
            "UPDATE reply_admissions SET outcome = ?, floor_active = CASE WHEN ? THEN 0 ELSE floor_active END, "
            "updated_at = CASE WHEN outcome != ? OR (? AND floor_active = 1) THEN ? ELSE updated_at END WHERE id = ?",
            (outcome, int(release), outcome, int(release), time.time(), row["id"]),
        )

    async def settle_reply_admission(
        self, event_id: int, outcome: str, *, release: bool = False,
        admission_id: int | None = None,
    ) -> None:
        if outcome not in {"processing", "context_ready", "dispatched", "generated", "pending", "sending",
                           "sent", "send_unknown", "execution_unknown", "retry_wait", "dry_run", "failed",
                           "rejected", "ignored", "dead_letter"}:
            raise ValueError("通知准入结果无效")
        if release and outcome in {"pending", "sending", "sent", "send_unknown", "execution_unknown"}:
            raise ValueError("不能释放待审、发送中、成功或未知的楼层")
        async with self.db.transaction() as connection:
            if outcome == "failed" and release:
                cursor = await connection.execute("SELECT status FROM incoming_events WHERE id = ?", (event_id,))
                event = await cursor.fetchone()
                if event and event["status"] == "retry_wait":
                    outcome, release = "retry_wait", False
            await self._settle_reply_admission(connection, event_id, outcome, release=release, admission_id=admission_id)

    async def restore_reply_admission(self, event_id: int, account_id: str, outcome: str, *,
                                      dedup_days: int = 365, completed_at: float | None = None) -> None:
        """Restore a trusted historical notification without inventing a charge."""
        if not account_id or account_id.startswith("dry-run:"):
            raise ValueError("历史通知缺少可信发送账号")
        if outcome not in {"pending", "sending", "sent", "send_unknown", "rejected", "failed", "dry_run"}:
            raise ValueError("历史审核状态无效")
        if type(dedup_days) is not int or dedup_days < 0:
            raise ValueError('去重保留期无效')
        async with self.db.transaction() as connection:
            cursor = await connection.execute("SELECT * FROM incoming_events WHERE id = ?", (event_id,))
            event = await cursor.fetchone()
            if event is None or event["event_type"] not in {"mention", "reply"}:
                return
            if event["reply_account_id"] and event["reply_account_id"] != account_id:
                raise ValueError("历史通知账号绑定冲突")
            await connection.execute("UPDATE incoming_events SET reply_account_id = ? WHERE id = ?", (account_id, event_id))
            # Only a verified review association can attribute pre-upgrade send facts.
            await connection.execute(
                "UPDATE outgoing_replies SET account_id=? WHERE incoming_event_id=? AND profile_id=? AND account_id=''",
                (account_id,event_id,event['profile_id']),
            )
            cursor = await connection.execute("SELECT 1 FROM reply_admissions WHERE incoming_event_id = ?", (event_id,))
            if await cursor.fetchone() is None:
                completed = event['completed_at']
                if not isinstance(completed, (int, float)) or not math.isfinite(completed) or completed <= 0:
                    completed = completed_at
                if (outcome == 'sent' and isinstance(completed, (int, float))
                        and math.isfinite(completed) and completed > 0
                        and completed < time.time()-dedup_days*86400):
                    return
                timestamp = float(event["claimed_at"] or event["discovered_at"])
                scope = json.dumps([event["post_id"], event["root_comment_id"] or None], separators=(",", ":"))
                active = outcome in {"pending", "sending", "sent", "send_unknown"}
                cursor = await connection.execute(
                    "SELECT 1 FROM reply_admissions WHERE account_id = ? AND scope_key = ? AND floor_active = 1",
                    (account_id, scope),
                )
                # Legacy duplicates share one blocking floor; every source keeps its association.
                active = active and await cursor.fetchone() is None
                await connection.execute(
                    "INSERT INTO reply_admissions(incoming_event_id, attempt_no, profile_id, account_id, sender_key, "
                    "day, charged, admitted_at, post_id, scope_key, floor_active, outcome, updated_at) "
                    "VALUES (?, 1, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                    (event_id, event["profile_id"], account_id,
                     f"uid:{event['sender_uid']}" if event["sender_uid"] else "unverified",
                     datetime.fromtimestamp(timestamp, UTC).date().isoformat(), timestamp,
                     event["post_id"], scope, int(active), outcome, timestamp),
                )
            await self._settle_reply_admission(
                connection, event_id, outcome, release=outcome in {"rejected", "failed", "dry_run"},
            )

    async def reserve_comment_attempt(
        self, profile_id: str, event_id: int | None, route: RoutingTarget, content: str, *,
        account_id: str, send_key: str, daily_limit: int,
    ) -> dict[str, Any]:
        if not isinstance(account_id, str) or not account_id.strip() or account_id.startswith("dry-run:"):
            raise ValueError("真实评论缺少可信发送账号")
        if not isinstance(send_key, str) or not send_key.strip() or route.profile_id != profile_id:
            raise ValueError("评论发送键或档案无效")
        if type(daily_limit) is not int or daily_limit < 0:
            raise ValueError("评论发送上限必须是非负整数")
        now = time.time()
        start = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        async with self.db.transaction() as connection:
            if event_id is not None:
                cursor = await connection.execute(
                    "SELECT profile_id, reply_account_id FROM incoming_events WHERE id = ?", (event_id,),
                )
                event = await cursor.fetchone()
                if event is None or event["profile_id"] != profile_id:
                    raise ValueError("评论来源事件归属不一致")
                if event["reply_account_id"] and event["reply_account_id"] != account_id:
                    return {"allowed": False, "created": False, "reason": "reply_account_changed"}
            cursor = await connection.execute(
                "SELECT * FROM outgoing_replies WHERE account_id = ? AND send_key = ? ORDER BY attempt_no DESC LIMIT 1",
                (account_id, send_key),
            )
            previous = await cursor.fetchone()
            if previous is not None:
                if (previous["profile_id"], previous["post_id"], previous["root_comment_id"],
                    previous["parent_comment_id"], previous["content_hash"]) != (
                    profile_id, route.post_id, route.root_comment_id, route.parent_comment_id, _content_hash(content),
                ):
                    raise ValueError("评论发送键与原目标或内容不一致")
                if previous["status"] != "retry_wait":
                    return {**dict(previous), "allowed": True, "created": False}
            if event_id is not None:
                cursor = await connection.execute(
                    "SELECT * FROM outgoing_replies WHERE incoming_event_id = ? "
                    "AND status IN ('sending', 'sent', 'send_unknown') ORDER BY id DESC LIMIT 1", (event_id,),
                )
                existing = await cursor.fetchone()
                if existing:
                    return {**dict(existing), "allowed": True, "created": False}
            cursor = await connection.execute(
                "SELECT COUNT(*) FROM outgoing_replies WHERE account_id = ? AND attempted_at >= ? "
                "AND attempted_at < ? AND status IN ('sending', 'sent', 'send_unknown')",
                (account_id, start, start + 86400),
            )
            count = (await cursor.fetchone())[0]
            if daily_limit and count >= daily_limit:
                return {"allowed": False, "created": False, "reason": "comment_account_daily_limit"}
            cursor = await connection.execute(
                "INSERT INTO outgoing_replies(profile_id, incoming_event_id, post_id, root_comment_id, "
                "parent_comment_id, content, content_hash, status, attempted_at, account_id, send_key, attempt_no) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'sending', ?, ?, ?, ?)",
                (profile_id, event_id, route.post_id, route.root_comment_id, route.parent_comment_id,
                 content, _content_hash(content), now, account_id, send_key,
                 previous["attempt_no"] + 1 if previous else 1),
            )
            return {"allowed": True, "created": True, "id": int(cursor.lastrowid),
                    "status": "sending", "attempted_at": now}

    async def refresh_comment_attempt(self, outgoing_id: int, *, account_id: str, daily_limit: int) -> dict[str, Any]:
        """Move only an unsent reservation to the actual request's UTC budget."""
        if not account_id or type(daily_limit) is not int or daily_limit < 0:
            raise ValueError('评论发送额度参数无效')
        now = time.time()
        start = datetime.fromtimestamp(now, UTC).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        async with self.db.transaction() as connection:
            cursor = await connection.execute('SELECT account_id,status,attempted_at FROM outgoing_replies WHERE id=?',(outgoing_id,))
            row = await cursor.fetchone()
            if row is None or row['account_id'] != account_id or row['status'] != 'sending':
                raise ValueError('评论发送尝试归属或阶段不一致')
            cursor = await connection.execute(
                "SELECT COUNT(*) FROM outgoing_replies WHERE account_id=? AND id!=? AND attempted_at>=? AND attempted_at<? AND status IN ('sending','sent','send_unknown')",
                (account_id,outgoing_id,start,start+86400),
            )
            count = (await cursor.fetchone())[0]
            if daily_limit and count >= daily_limit:
                return {'allowed':False,'attempted_at':row['attempted_at'],'reason':'comment_account_daily_limit'}
            await connection.execute('UPDATE outgoing_replies SET attempted_at=? WHERE id=?',(now,outgoing_id))
            return {'allowed':True,'attempted_at':now,'reason':''}

    async def reply_budget_status(self, account_id: str) -> dict[str, Any]:
        now = time.time()
        start = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) FROM reply_admissions WHERE account_id = ? AND day = ? AND charged = 1", (account_id, day),
            )
            processing = (await cursor.fetchone())[0]
            cursor = await connection.execute(
                "SELECT COUNT(*) FROM outgoing_replies WHERE account_id = ? AND attempted_at >= ? "
                "AND attempted_at < ? AND status IN ('sending', 'sent', 'send_unknown')", (account_id, start, start + 86400),
            )
            comments = (await cursor.fetchone())[0]
        return {"account_id": account_id, "day": day, "processing_count": processing,
                "comment_count": comments, "reset_at": start + 86400}

    async def mark_event(
        self,
        event_id: int,
        state: EventState,
        *,
        reply_text: str | None = None,
        error: str = "",
        generated_ms: int | None = None,
        should_filter: bool | None = None,
        admission_id: int | None = None,
        reason: str = '',
    ) -> bool:
        now = time.time()
        completed_at = now if state.value in FINAL_STATES else None
        reply_hash = _content_hash(reply_text) if reply_text else ""
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT id, outcome FROM reply_admissions WHERE incoming_event_id = ? ORDER BY attempt_no DESC LIMIT 1",
                (event_id,),
            )
            admission = await cursor.fetchone()
            if admission_id is not None and (admission is None or admission['id'] != admission_id):
                return False
            if admission and admission["outcome"] in {"pending", "sending"} and state in {
                EventState.CLAIMED, EventState.CONTEXT_READY, EventState.DISPATCHED,
                EventState.GENERATED, EventState.RETRY_WAIT, EventState.DEAD_LETTER,
                EventState.IGNORED, EventState.DRY_RUN,
            }:
                return False
            cursor = await connection.execute(
            """
            UPDATE incoming_events
            SET status = ?,
                reply_text = COALESCE(?, reply_text),
                reply_hash = CASE WHEN ? != '' THEN ? ELSE reply_hash END,
                error = ?, reason = ?,
                generated_ms = COALESCE(?, generated_ms),
                should_filter = COALESCE(?, should_filter),
                completed_at = COALESCE(?, completed_at),
                updated_at = ?
            WHERE id = ? AND (
                status NOT IN ('sent', 'send_unknown', 'ignored', 'dead_letter', 'dry_run')
                OR status = ? OR (status = 'send_unknown' AND ? = 'sent')
            )
            """,
            (
                state.value,
                reply_text,
                reply_hash,
                reply_hash,
                redact_text(error)[:2000],
                reason,
                generated_ms,
                int(should_filter) if should_filter is not None else None,
                completed_at,
                now,
                event_id,
                state.value,
                state.value,
            ),
            )
            if cursor.rowcount:
                await self._settle_reply_admission(
                    connection, event_id, state.value,
                    release=state in {EventState.DRY_RUN, EventState.IGNORED, EventState.DEAD_LETTER},
                )
            return cursor.rowcount == 1

    async def schedule_retry(
        self,
        event_id: int,
        error: str,
        *,
        max_retries: int,
        admission_id: int | None = None,
    ) -> EventState:
        now = time.time()
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT retry_count, status FROM incoming_events WHERE id = ?",
                (event_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError("待重试事件不存在")
            current_state = str(row["status"])
            cursor = await connection.execute(
                "SELECT id, outcome FROM reply_admissions WHERE incoming_event_id = ? ORDER BY attempt_no DESC LIMIT 1",
                (event_id,),
            )
            admission = await cursor.fetchone()
            if admission_id is not None and (admission is None or admission['id'] != admission_id):
                return EventState(current_state)
            if admission and admission["outcome"] in {
                "pending", "sending", "sent", "send_unknown", "execution_unknown", "rejected", "failed",
            }:
                return EventState(current_state)
            if current_state in {
                EventState.RETRY_WAIT.value,
                EventState.SEND_UNKNOWN.value,
                EventState.SENT.value,
                EventState.DRY_RUN.value,
                EventState.IGNORED.value,
                EventState.DEAD_LETTER.value,
            }:
                return EventState(current_state)
            retry_count = int(row["retry_count"])
            if retry_count >= max(0, max_retries):
                await connection.execute(
                    """
                    UPDATE incoming_events
                    SET status = 'dead_letter', error = ?, completed_at = ?,
                        next_retry_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (redact_text(error)[:2000], now, now, event_id),
                )
                await self._settle_reply_admission(connection, event_id, "dead_letter", release=True)
                return EventState.DEAD_LETTER
            delay = min(1800, 30 * (2**retry_count))
            await connection.execute(
                """
                UPDATE incoming_events
                SET status = 'retry_wait', error = ?, retry_count = retry_count + 1,
                    next_retry_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (redact_text(error)[:2000], now + delay, now, event_id),
            )
            await self._settle_reply_admission(connection, event_id, "retry_wait")
            return EventState.RETRY_WAIT

    async def defer_event(self, event_id: int, error: str, *, delay_seconds: float) -> None:
        now = time.time()
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT outcome FROM reply_admissions WHERE incoming_event_id = ? ORDER BY attempt_no DESC LIMIT 1",
                (event_id,),
            )
            admission = await cursor.fetchone()
            if admission and admission["outcome"] in {"pending", "sending", "sent", "send_unknown", "execution_unknown"}:
                return
            cursor = await connection.execute(
            """
            UPDATE incoming_events
            SET status = 'retry_wait', error = ?, next_retry_at = ?,
                completed_at = NULL, updated_at = ?
            WHERE id = ? AND status NOT IN ('sent', 'send_unknown', 'ignored', 'dead_letter', 'dry_run')
            """,
            (
                redact_text(error)[:2000],
                now + max(1.0, delay_seconds),
                now,
                event_id,
            ),
            )
            if cursor.rowcount:
                await self._settle_reply_admission(connection, event_id, "retry_wait")

    async def recoverable_events(
        self, limit: int = 50, *, profile_id: str = ""
    ) -> list[dict[str, Any]]:
        now = time.time()
        rows = await self.db.fetchall(
            """
            SELECT * FROM incoming_events
            WHERE (? = '' OR profile_id = ?)
              AND (
                status IN ('claimed', 'context_ready', 'dispatched')
                OR (status = 'retry_wait' AND COALESCE(next_retry_at, 0) <= ?)
              )
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (profile_id, profile_id, now, max(1, min(limit, 500))),
        )
        return [dict(row) for row in rows]

    async def due_retry_events(
        self, limit: int = 50, *, profile_id: str = ""
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM incoming_events
            WHERE (? = '' OR profile_id = ?)
              AND status = 'retry_wait'
              AND COALESCE(next_retry_at, 0) <= ?
            ORDER BY next_retry_at ASC, updated_at ASC
            LIMIT ?
            """,
            (
                profile_id,
                profile_id,
                time.time(),
                max(1, min(limit, 500)),
            ),
        )
        return [dict(row) for row in rows]

    async def is_self_comment(self, profile_id: str, external_comment_id: str) -> bool:
        row = await self.db.fetchone(
            """
            SELECT 1 FROM self_comment_ids
            WHERE profile_id = ? AND external_comment_id = ?
            """,
            (profile_id, external_comment_id),
        )
        return row is not None

    async def cache_visual_context(
        self,
        *,
        profile_id: str,
        post_id: str,
        source: str,
        image_fingerprint: str,
        image_hosts: list[str],
        image_count: int,
        caption: str,
        provider_id: str,
        model: str,
        ttl_seconds: int,
        owner_uid: str = "",
        owner_nickname: str = "",
        owner_role: str = "unknown",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Reuse or persist one immutable, bounded visual description."""
        current = float(now if now is not None else time.time())
        safe_ttl = max(60, min(int(ttl_seconds), 7 * 86400))
        safe_caption = str(caption or "").strip()[:4000]
        if not safe_caption:
            raise ValueError("视觉上下文描述不能为空")
        if not profile_id or not post_id or not image_fingerprint:
            raise ValueError("视觉上下文缺少账号、帖子或图片指纹")
        safe_owner_uid = str(owner_uid or "")[:80]
        safe_owner_nickname = str(owner_nickname or "")[:80]
        safe_owner_role = str(owner_role or "unknown")[:32]
        if safe_owner_role not in {"current_sender", "post_author", "unknown"}:
            safe_owner_role = "unknown"
        caption_hash = hashlib.sha256(safe_caption.encode("utf-8")).hexdigest()
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM visual_contexts
                WHERE profile_id = ? AND post_id = ? AND source = ?
                  AND image_fingerprint = ? AND caption_hash = ? AND expires_at > ?
                  AND owner_uid = ? AND owner_nickname = ? AND owner_role = ?
                ORDER BY expires_at DESC, id DESC
                LIMIT 1
                """,
                (
                    profile_id,
                    post_id,
                    source,
                    image_fingerprint,
                    caption_hash,
                    current,
                    safe_owner_uid,
                    safe_owner_nickname,
                    safe_owner_role,
                ),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                return dict(existing)
            insert = await connection.execute(
                """
                INSERT INTO visual_contexts(
                    profile_id, post_id, source, image_fingerprint,
                    image_hosts_json, image_count, caption, caption_hash,
                    provider_id, model, created_at, expires_at,
                    owner_uid, owner_nickname, owner_role
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    post_id,
                    source,
                    image_fingerprint,
                    json.dumps(image_hosts[:20], ensure_ascii=False, separators=(",", ":")),
                    max(0, min(int(image_count), 20)),
                    safe_caption,
                    caption_hash,
                    str(provider_id or "")[:256],
                    str(model or "")[:256],
                    current,
                    current + safe_ttl,
                    safe_owner_uid,
                    safe_owner_nickname,
                    safe_owner_role,
                ),
            )
            if insert.lastrowid is None:
                raise RuntimeError("视觉上下文写入未返回记录 ID")
            visual_context_id = int(insert.lastrowid)
            row = await connection.execute(
                "SELECT * FROM visual_contexts WHERE id = ?",
                (visual_context_id,),
            )
            created = await row.fetchone()
            if created is None:
                raise RuntimeError("视觉上下文写入后无法读取")
            return dict(created)

    async def link_visual_context_to_event(
        self,
        incoming_event_id: int,
        visual_context_id: int,
        *,
        now: float | None = None,
    ) -> bool:
        current = float(now if now is not None else time.time())
        result = await self.db.execute(
            """
            INSERT INTO visual_context_event_links(
                incoming_event_id, visual_context_id, created_at
            )
            SELECT ?, id, ? FROM visual_contexts
            WHERE id = ? AND expires_at > ?
            ON CONFLICT(incoming_event_id) DO UPDATE SET
                visual_context_id = excluded.visual_context_id,
                created_at = excluded.created_at
            """,
            (int(incoming_event_id), current, int(visual_context_id), current),
        )
        return result.rowcount > 0

    async def visual_context_for_comment(
        self,
        profile_id: str,
        external_comment_ids: list[str] | tuple[str, ...],
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        candidates = tuple(
            dict.fromkeys(
                str(value).strip() for value in external_comment_ids if str(value).strip()
            )
        )
        if not profile_id or not candidates:
            return None
        current = float(now if now is not None else time.time())
        placeholders = ",".join("?" for _ in candidates)
        priority_sql = " ".join(f"WHEN ? THEN {index}" for index, _ in enumerate(candidates))
        row = await self.db.fetchone(
            f"""
            SELECT visual_contexts.*,
                   visual_context_comment_links.external_comment_id AS bound_comment_id
            FROM visual_context_comment_links
            JOIN visual_contexts
              ON visual_contexts.id = visual_context_comment_links.visual_context_id
            WHERE visual_context_comment_links.profile_id = ?
              AND visual_context_comment_links.external_comment_id IN ({placeholders})
              AND visual_contexts.expires_at > ?
            ORDER BY CASE visual_context_comment_links.external_comment_id
                         {priority_sql}
                         ELSE {len(candidates)}
                     END,
                     visual_context_comment_links.created_at DESC
            LIMIT 1
            """,
            (profile_id, *candidates, current, *candidates),
        )
        return dict(row) if row is not None else None

    async def visual_context_for_post(
        self,
        profile_id: str,
        post_id: str,
        image_fingerprint: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        if not profile_id or not post_id or not image_fingerprint:
            return None
        current = float(now if now is not None else time.time())
        row = await self.db.fetchone(
            """
            SELECT * FROM visual_contexts
            WHERE profile_id = ? AND post_id = ? AND source = 'original_post'
              AND image_fingerprint = ? AND expires_at > ?
            ORDER BY expires_at DESC, id DESC
            LIMIT 1
            """,
            (profile_id, post_id, image_fingerprint, current),
        )
        return dict(row) if row is not None else None

    async def record_outgoing_attempt(
        self,
        profile_id: str,
        event_id: int | None,
        route: RoutingTarget,
        content: str,
        status: str,
        *,
        error: str = "",
        external_comment_id: str = "",
    ) -> int:
        now = time.time()
        cursor = await self.db.execute(
            """
            INSERT INTO outgoing_replies(
                profile_id, incoming_event_id, post_id, root_comment_id,
                parent_comment_id, external_comment_id, content, content_hash,
                status, attempted_at, confirmed_at, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                event_id,
                route.post_id,
                route.root_comment_id,
                route.parent_comment_id,
                external_comment_id,
                content,
                _content_hash(content),
                status,
                now,
                now if status == EventState.SENT.value else None,
                redact_text(error)[:2000],
            ),
        )
        return int(cursor.lastrowid)

    async def latest_outgoing_for_event(self, event_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT * FROM outgoing_replies
            WHERE incoming_event_id = ?
            ORDER BY attempted_at DESC, id DESC
            LIMIT 1
            """,
            (event_id,),
        )
        return dict(row) if row is not None else None

    async def confirm_outgoing(
        self,
        outgoing_id: int,
        external_comment_id: str,
    ) -> dict[str, Any]:
        row = await self.db.fetchone(
            """
            SELECT outgoing_replies.profile_id,
                   outgoing_replies.incoming_event_id,
                   outgoing_replies.post_id,
                   outgoing_replies.root_comment_id,
                   outgoing_replies.content_hash,
                   visual_context_event_links.visual_context_id
            FROM outgoing_replies
            LEFT JOIN visual_context_event_links
              ON visual_context_event_links.incoming_event_id =
                 outgoing_replies.incoming_event_id
            WHERE outgoing_replies.id = ?
            """,
            (outgoing_id,),
        )
        if row is None:
            raise ValueError("待确认发送记录不存在")
        now = time.time()
        async with self.db.transaction() as connection:
            await connection.execute(
                """
                UPDATE outgoing_replies
                SET external_comment_id = ?, status = 'sent', confirmed_at = ?, error = ''
                WHERE id = ?
                """,
                (external_comment_id, now, outgoing_id),
            )
            if external_comment_id:
                await connection.execute(
                    """
                    INSERT OR IGNORE INTO self_comment_ids(
                        profile_id, external_comment_id, post_id, root_comment_id,
                        content_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["profile_id"],
                        external_comment_id,
                        row["post_id"],
                        row["root_comment_id"],
                        row["content_hash"],
                        now,
                    ),
                )
        result: dict[str, Any] = {
            "incoming_event_id": row["incoming_event_id"],
            "profile_id": row["profile_id"],
            "visual_context_id": None,
            "visual_context_error": "",
        }
        if external_comment_id and row["visual_context_id"] is not None:
            try:
                visual_context_id = int(row["visual_context_id"])
                await self.bind_visual_context_to_comment(
                    str(row["profile_id"]),
                    external_comment_id,
                    visual_context_id,
                    now=now,
                )
                result["visual_context_id"] = visual_context_id
            except Exception as exc:
                # Sending has already been durably confirmed. Visual context is
                # optional metadata and must never roll back or misclassify a
                # successful Xiaoheihe comment.
                result["visual_context_error"] = redact_text(str(exc))[:1000]
                result["visual_context_exception_type"] = type(exc).__name__
        return result

    async def bind_visual_context_to_comment(
        self,
        profile_id: str,
        external_comment_id: str,
        visual_context_id: int,
        *,
        now: float | None = None,
    ) -> None:
        if not external_comment_id:
            return
        current = float(now if now is not None else time.time())
        await self.db.execute(
            """
            INSERT INTO visual_context_comment_links(
                profile_id, external_comment_id, visual_context_id, created_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(profile_id, external_comment_id) DO UPDATE SET
                visual_context_id = excluded.visual_context_id,
                created_at = excluded.created_at
            """,
            (profile_id, external_comment_id, visual_context_id, current),
        )

    async def recent_outgoing_match(
        self,
        profile_id: str,
        route: RoutingTarget,
        content: str,
        *,
        since: float,
    ) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT * FROM outgoing_replies
            WHERE profile_id = ? AND post_id = ? AND root_comment_id = ?
              AND content_hash = ? AND attempted_at >= ?
            ORDER BY attempted_at DESC LIMIT 1
            """,
            (
                profile_id,
                route.post_id,
                route.root_comment_id,
                _content_hash(content),
                since,
            ),
        )
        return dict(row) if row else None

    async def has_replied_to_post(self, profile_id: str, post_id: str) -> bool:
        row = await self.db.fetchone(
            """
            SELECT 1 FROM outgoing_replies
            WHERE profile_id = ? AND post_id = ? AND status = 'sent'
            LIMIT 1
            """,
            (profile_id, post_id),
        )
        if row is not None:
            return True
        row = await self.db.fetchone(
            """
            SELECT 1 FROM self_comment_ids
            WHERE profile_id = ? AND post_id = ?
            LIMIT 1
            """,
            (profile_id, post_id),
        )
        return row is not None

    async def record_session(self, route: RoutingTarget) -> None:
        await self.db.execute(
            """
            INSERT INTO session_mappings(
                profile_id, session_id, post_id, root_comment_id, last_used_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(profile_id, session_id) DO UPDATE SET
                post_id = excluded.post_id,
                root_comment_id = excluded.root_comment_id,
                last_used_at = excluded.last_used_at
            """,
            (
                route.profile_id,
                route.session_id,
                route.post_id,
                route.root_comment_id,
                time.time(),
            ),
        )

    async def update_account_state(
        self, profile_id: str, **fields: str | int | float | None
    ) -> None:
        allowed = {
            "status",
            "uid",
            "nickname",
            "login_at",
            "last_login_check_at",
            "last_poll_at",
            "last_success_request_at",
            "last_error",
            "consecutive_401",
            "consecutive_403",
            "consecutive_429",
            "consecutive_poll_failures",
            "circuit_open_until",
        }
        safe_fields = {key: value for key, value in fields.items() if key in allowed}
        if "last_error" in safe_fields and safe_fields["last_error"] is not None:
            safe_fields["last_error"] = redact_text(str(safe_fields["last_error"]))[:2000]
        if not safe_fields:
            return
        keys = list(safe_fields)
        columns = ", ".join(f"{key} = ?" for key in keys)
        values = [safe_fields[key] for key in keys]
        async with self.db.transaction() as connection:
            await connection.execute(
                """
                INSERT OR IGNORE INTO account_state(profile_id, status)
                VALUES (?, 'idle')
                """,
                (profile_id,),
            )
            await connection.execute(
                f"UPDATE account_state SET {columns}, updated_at = CURRENT_TIMESTAMP "
                "WHERE profile_id = ?",
                (*values, profile_id),
            )

    async def account_state(self, profile_id: str) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT * FROM account_state WHERE profile_id = ?", (profile_id,)
        )
        return dict(row) if row else {"profile_id": profile_id, "status": "idle"}

    async def add_runtime_error(
        self,
        category: str,
        message: str,
        *,
        profile_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO runtime_errors(
                profile_id, category, message, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                category,
                redact_text(message)[:2000],
                json.dumps(redact_data(details or {}), ensure_ascii=False),
                time.time(),
            ),
        )

    async def increment_counter(
        self, profile_id: str, *, reply: int = 0, proactive: int = 0, error: int = 0
    ) -> None:
        day = datetime.now(UTC).date().isoformat()
        await self.db.execute(
            """
            INSERT INTO daily_counters(
                profile_id, day, reply_count, proactive_count, error_count
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(profile_id, day) DO UPDATE SET
                reply_count = reply_count + excluded.reply_count,
                proactive_count = proactive_count + excluded.proactive_count,
                error_count = error_count + excluded.error_count
            """,
            (profile_id, day, reply, proactive, error),
        )

    async def reserve_proactive_request(self, profile_id: str, daily_limit: int) -> bool:
        """Atomically reserve one proactive AI-generation request for today."""
        day = datetime.now(UTC).date().isoformat()
        async with self.db.transaction() as connection:
            async with connection.execute(
                """
                SELECT proactive_count FROM daily_counters
                WHERE profile_id = ? AND day = ?
                """,
                (profile_id, day),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None and int(row["proactive_count"]) >= daily_limit:
                return False
            await connection.execute(
                """
                INSERT INTO daily_counters(
                    profile_id, day, reply_count, proactive_count, error_count
                ) VALUES (?, ?, 0, 1, 0)
                ON CONFLICT(profile_id, day) DO UPDATE SET
                    proactive_count = proactive_count + 1
                """,
                (profile_id, day),
            )
            return True

    async def today_counters(self, profile_id: str) -> dict[str, int]:
        day = datetime.now(UTC).date().isoformat()
        row = await self.db.fetchone(
            """
            SELECT reply_count, proactive_count, error_count
            FROM daily_counters WHERE profile_id = ? AND day = ?
            """,
            (profile_id, day),
        )
        return (
            {
                "reply_count": int(row["reply_count"]),
                "proactive_count": int(row["proactive_count"]),
                "error_count": int(row["error_count"]),
            }
            if row
            else {"reply_count": 0, "proactive_count": 0, "error_count": 0}
        )

    async def has_profile_events(self, profile_id: str) -> bool:
        return await self.db.fetchone('SELECT 1 FROM incoming_events WHERE profile_id=? LIMIT 1', (profile_id,)) is not None

    async def risk_user(self, profile_id: str, uid: str, *, risk_config=None, owner_uid: str = '') -> dict[str, Any]:
        # The shared SQLite connection must not expose an uncommitted unblock.
        async with self.db.transaction() as connection:
            cursor = await connection.execute('SELECT * FROM user_risk WHERE profile_id=? AND sender_uid=?', (profile_id, uid))
            row = await cursor.fetchone()
        return risk_snapshot(profile_id, uid, row, risk_config, owner_uid=owner_uid)

    risk_user_snapshot = risk_user

    async def list_risk_users(self, *, profile_id='', uid='', level='', manual_blocked=None,
                              page=1, page_size=30, risk_config=None) -> dict[str, Any]:
        config = risk_config or {}
        where, args = ['1=1'], []
        for column, value in (('profile_id', profile_id), ('sender_uid', uid)):
            if value:
                where.append(f'{column}=?')
                args.append(value)
        if manual_blocked is not None:
            where.append('manual_blocked=?')
            args.append(int(manual_blocked))
        if level not in {'', 'normal', 'watch', 'blocked'}:
            raise ValueError('风险等级无效')
        if level:
            where.append("(CASE WHEN score >= ? THEN 'blocked' WHEN score >= ? THEN 'watch' ELSE 'normal' END)=?")
            args.extend((config.get('auto_filter_score', 60), config.get('watch_score', 20), level))
        predicate = ' AND '.join(where)
        page, page_size = max(1, page), max(1, min(100, page_size))
        count = await self.db.fetchone(f'SELECT COUNT(*) FROM user_risk WHERE {predicate}', args)
        rows = await self.db.fetchall(f'SELECT * FROM user_risk WHERE {predicate} ORDER BY updated_at DESC,profile_id,sender_uid LIMIT ? OFFSET ?', (*args, page_size, (page-1)*page_size))
        return {'items': [risk_snapshot(r['profile_id'], r['sender_uid'], r, config) for r in rows],
                'total': count[0], 'page': page, 'page_size': page_size}

    async def risk_user_detail(self, profile_id, uid, *, page=1, page_size=30, risk_config=None):
        page, page_size = max(1, page), max(1, min(100, page_size))
        count = await self.db.fetchone('SELECT COUNT(*) FROM user_risk_events WHERE profile_id=? AND sender_uid=?', (profile_id, uid))
        rows = await self.db.fetchall('SELECT * FROM user_risk_events WHERE profile_id=? AND sender_uid=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (profile_id, uid, page_size, (page-1)*page_size))
        items = []
        for row in rows:
            item = dict(row)
            item['details'] = json.loads(item['details'])
            items.append(redact_data(item))
        return {'user': await self.risk_user(profile_id, uid, risk_config=risk_config),
                'events': {'items': items, 'total': count[0], 'page': page, 'page_size': page_size}}

    async def manage_risk_user(self, profile_id, uid, *, action, note='', operator='', delta=0,
                               reset_score=False, risk_config=None):
        if (not isinstance(uid, str) or not uid.isascii() or not uid.isdigit() or not uid.strip('0') or len(uid)>32
                or action not in {'block','unblock','adjust'} or not isinstance(note, str) or len(note)>200
                or type(delta) is not int or not -1000 <= delta <= 1000 or type(reset_score) is not bool
                or (reset_score and action != 'unblock')):
            raise ValueError('风险操作参数无效')
        note, operator = redact_text(note)[:200], redact_text(str(operator))[:200]
        now = time.time()
        async with self.db.transaction() as connection:
            cursor = await connection.execute('SELECT * FROM user_risk WHERE profile_id=? AND sender_uid=?', (profile_id, uid))
            row = await cursor.fetchone()
            before = int(row['score']) if row else 0
            blocked = bool(row['manual_blocked']) if row else False
            score = max(0, min(1000, before+delta)) if action=='adjust' else 0 if reset_score else before
            new_blocked = True if action=='block' else False if action=='unblock' else blocked
            changed = score != before or new_blocked != blocked
            if changed:
                await connection.execute('INSERT INTO user_risk(profile_id,sender_uid,score,manual_blocked,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(profile_id,sender_uid) DO UPDATE SET score=excluded.score,manual_blocked=excluded.manual_blocked,note=excluded.note,updated_at=excluded.updated_at', (profile_id, uid, score, int(new_blocked), note, now, now))
                # Clearing a score is independently visible even when unblock also changes state.
                changes = []
                if blocked != new_blocked:
                    changes.append((action, 0, before, before))
                if score != before:
                    changes.append(('reset' if reset_score else 'adjust', score-before, before, score))
                for rule, actual, old, new in changes:
                    await connection.execute('INSERT INTO user_risk_events(profile_id,sender_uid,rule,delta,before_score,after_score,details,created_at,operator) VALUES(?,?,?,?,?,?,?,?,?)', (profile_id, uid, rule, actual, old, new, json.dumps({'note': note}, ensure_ascii=False), now, operator))
                cursor = await connection.execute('SELECT * FROM user_risk WHERE profile_id=? AND sender_uid=?', (profile_id, uid))
                row = await cursor.fetchone()
            return {'user': risk_snapshot(profile_id, uid, row, risk_config), 'changed': changed}

    async def evaluate_event_risk(self, event_id: int, *, risk_config=None, owner_uid='', self_uid='') -> dict[str, Any]:
        config = risk_config or {}
        window = config.get('flood_window_minutes', 10)*60
        async with self.db.transaction() as connection:
            now = time.time()
            cursor = await connection.execute('SELECT * FROM incoming_events WHERE id=?', (event_id,))
            event = await cursor.fetchone()
            if event is None:
                raise ValueError('风险事件不存在')
            profile_id, uid = event['profile_id'], event['sender_uid'].strip()
            cursor = await connection.execute('SELECT * FROM user_risk WHERE profile_id=? AND sender_uid=?', (profile_id, uid))
            row = await cursor.fetchone()
            snapshot = risk_snapshot(profile_id, uid, row, config, owner_uid=owner_uid)
            state = event['risk_state']
            if state == 'pending':
                skip = ('disabled' if not config.get('enabled', True) else 'uid' if not uid else
                        'source' if event['event_type'] not in {'mention','reply'} else
                        'self' if self_uid and uid == self_uid else 'owner' if snapshot['owner_exempt'] else '')
                if skip:
                    state = f'skipped_{skip}'
                    await connection.execute('UPDATE incoming_events SET risk_state=? WHERE id=?', (state, event_id))
                else:
                    hits = []
                    count = 0
                    if event['risk_behavior_eligible']:
                        params = (profile_id, uid, event['discovered_at']-window, event['discovered_at'], event['discovered_at'], event_id)
                        predicate = "profile_id=? AND sender_uid=? AND risk_behavior_eligible=1 AND event_type IN ('mention','reply') AND discovered_at>? AND (discovered_at<? OR (discovered_at=? AND id<=?))"
                        cursor = await connection.execute(f'SELECT COUNT(*) FROM incoming_events WHERE {predicate}', params)
                        count = (await cursor.fetchone())[0]
                        if count >= config.get('flood_threshold', 8):
                            hits.append('flood_burst')
                        cursor = await connection.execute(f'SELECT risk_content_hash,risk_text_eligible FROM incoming_events WHERE {predicate} ORDER BY discovered_at DESC,id DESC LIMIT 3', params)
                        latest = await cursor.fetchall()
                        if len(latest)==3 and event['risk_content_hash'] and all(r['risk_text_eligible'] and r['risk_content_hash']==event['risk_content_hash'] for r in latest):
                            hits.append('flood_repeat')
                    if event['risk_text_eligible']:
                        hits.extend(text_hits(event['content'] or ''))
                    score, total_delta = snapshot['score'], 0
                    applied = []
                    for rule in hits:
                        points = config.get({'flood_burst':'burst_score','flood_repeat':'repeat_score','inject_override':'injection_score','privacy_exfiltration':'privacy_score'}[rule], {'flood_burst':20,'flood_repeat':15,'inject_override':25,'privacy_exfiltration':30}[rule])
                        if not points:
                            continue
                        key = rule if rule=='flood_burst' else f"{rule}:{event['risk_content_hash']}"
                        cursor = await connection.execute('SELECT 1 FROM user_risk_events WHERE profile_id=? AND sender_uid=? AND rule_key=? AND created_at>? LIMIT 1', (profile_id, uid, key, now-window))
                        if await cursor.fetchone():
                            continue
                        after = min(1000, score+points)
                        await connection.execute('INSERT INTO user_risk_events(profile_id,sender_uid,incoming_event_id,rule,delta,before_score,after_score,rule_key,rule_version,details,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (profile_id, uid, event_id, rule, after-score, score, after, key, RULE_VERSION, json.dumps({'window_count':count,'window_seconds':window,'content_hash':event['risk_content_hash'] if rule!='flood_burst' else ''}), now))
                        total_delta += after-score
                        score = after
                        applied.append(rule)
                    if applied:
                        await connection.execute('INSERT INTO user_risk(profile_id,sender_uid,score,last_hit_at,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(profile_id,sender_uid) DO UPDATE SET score=excluded.score,last_hit_at=excluded.last_hit_at,updated_at=excluded.updated_at', (profile_id, uid, score, now, now, now))
                    await connection.execute("UPDATE incoming_events SET risk_state='evaluated',risk_evaluated_at=?,risk_delta=?,risk_score_after=?,risk_hits=? WHERE id=?", (now,total_delta,score,json.dumps(hits),event_id))
                    state = 'evaluated'
                    cursor = await connection.execute('SELECT * FROM user_risk WHERE profile_id=? AND sender_uid=?', (profile_id, uid))
                    row = await cursor.fetchone()
            result = risk_snapshot(profile_id, uid, row, config, owner_uid=owner_uid)
            result['risk_state'] = state
            return result

    async def list_events(
        self,
        *,
        status: str = "",
        uid: str = "",
        post_id: str = "",
        keyword: str = "",
        start_time: float | None = None,
        end_time: float | None = None,
        page: int = 1,
        page_size: int = 30,
        group_by: str = '',
        profile_id: str = '',
        event_id: int | None = None,
        risk_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        where = ["1 = 1"]
        parameters: list[Any] = []
        if group_by not in {'', 'uid'}:
            raise ValueError('无效分组')
        if event_id is not None:
            if type(event_id) is not int or not 0 < event_id <= 9223372036854775807:
                raise ValueError('无效事件 ID')
            where.append('id = ?')
            parameters.append(event_id)
        if profile_id:
            where.append('profile_id = ?')
            parameters.append(profile_id)
        if any(value is not None and not math.isfinite(float(value)) for value in (start_time, end_time)):
            raise ValueError('无效时间')
        if start_time is not None and end_time is not None and start_time > end_time:
            raise ValueError('无效时间范围')
        valid_status = {state.value for state in EventState}
        if status:
            if status not in valid_status:
                raise ValueError("无效事件状态")
            where.append("status = ?")
            parameters.append(status)
        if uid:
            where.append("sender_uid = ?")
            parameters.append(str(uid))
        if post_id:
            where.append("post_id = ?")
            parameters.append(str(post_id))
        if keyword:
            where.append("(content LIKE ? OR reply_text LIKE ?)")
            pattern = f"%{keyword[:100]}%"
            parameters.extend((pattern, pattern))
        if start_time is not None:
            where.append("COALESCE(completed_at, updated_at, discovered_at) >= ?")
            parameters.append(float(start_time))
        if end_time is not None:
            where.append("COALESCE(completed_at, updated_at, discovered_at) <= ?")
            parameters.append(float(end_time))
        safe_size = max(1, min(page_size, 100))
        safe_page = max(1, page)
        predicate = " AND ".join(where)
        if group_by:
            return await self._group_events(predicate, parameters, safe_page, safe_size, risk_config)
        count_row = await self.db.fetchone(
            f"SELECT COUNT(*) AS count FROM incoming_events WHERE {predicate}",
            parameters,
        )
        rows = await self.db.fetchall(
            f"""
            SELECT id, profile_id, external_event_id, external_comment_id,
                   notification_id, event_type, status, sender_uid,
                   sender_nickname, post_id, root_comment_id, parent_comment_id,
                   content, reply_text, error, should_filter, discovered_at,
                   updated_at, completed_at,
                   COALESCE(completed_at, updated_at, discovered_at) AS event_time,
                   generated_ms, retry_count, next_retry_at, reason,
                   risk_state,risk_evaluated_at,risk_delta,risk_score_after,risk_hits,risk_behavior_eligible
            FROM incoming_events
            WHERE {predicate}
            ORDER BY COALESCE(completed_at, updated_at, discovered_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*parameters, safe_size, (safe_page - 1) * safe_size),
        )
        return {
            "items": [dict(row) for row in rows],
            "total": int(count_row["count"]) if count_row else 0,
            "page": safe_page,
            "page_size": safe_size,
        }

    async def _group_events(self, predicate, parameters, page, page_size, risk_config):
        base = f"""WITH filtered AS (
            SELECT id,profile_id,sender_nickname,post_id,discovered_at,status,event_type,risk_state,
                   py_strip(sender_uid) AS uid,
                   CASE WHEN py_strip(sender_uid)='' THEN id ELSE 0 END AS anonymous_id,
                   COALESCE(completed_at,updated_at,discovered_at) AS event_time
            FROM incoming_events WHERE {predicate}
        )"""
        count = await self.db.fetchone(base + ' SELECT COUNT(*) FROM (SELECT 1 FROM filtered GROUP BY profile_id,uid,anonymous_id)', parameters)
        rows = await self.db.fetchall(base + """,
            groups AS (
                SELECT profile_id,uid,anonymous_id,COUNT(*) AS event_count,
                       SUM(risk_state='evaluated') AS risk_evaluated_count,
                       COUNT(DISTINCT post_id) AS post_count,MAX(event_time) AS last_event_at,
                       MAX(discovered_at) AS last_discovered_at
                FROM filtered GROUP BY profile_id,uid,anonymous_id
                ORDER BY last_event_at DESC,profile_id,uid,anonymous_id LIMIT ? OFFSET ?
            ), selected AS (
                SELECT f.* FROM filtered f JOIN groups g
                ON f.profile_id=g.profile_id AND f.uid=g.uid AND f.anonymous_id=g.anonymous_id
            ), names AS (
                SELECT profile_id,uid,anonymous_id,sender_nickname,
                       ROW_NUMBER() OVER (PARTITION BY profile_id,uid,anonymous_id ORDER BY discovered_at DESC,id DESC) AS n
                FROM selected
            ), statuses AS (
                SELECT profile_id,uid,anonymous_id,json_group_object(status,cnt) AS counts FROM (
                    SELECT profile_id,uid,anonymous_id,status,COUNT(*) cnt FROM selected
                    GROUP BY profile_id,uid,anonymous_id,status
                ) GROUP BY profile_id,uid,anonymous_id
            ), types AS (
                SELECT profile_id,uid,anonymous_id,json_group_object(event_type,cnt) AS counts FROM (
                    SELECT profile_id,uid,anonymous_id,event_type,COUNT(*) cnt FROM selected
                    GROUP BY profile_id,uid,anonymous_id,event_type
                ) GROUP BY profile_id,uid,anonymous_id
            )
            SELECT g.*,n.sender_nickname,s.counts AS status_counts,t.counts AS event_type_counts,
                   r.score,r.manual_blocked,r.note,r.last_hit_at,r.created_at,r.updated_at
            FROM groups g JOIN names n USING(profile_id,uid,anonymous_id)
            JOIN statuses s USING(profile_id,uid,anonymous_id)
            JOIN types t USING(profile_id,uid,anonymous_id)
            LEFT JOIN user_risk r ON r.profile_id=g.profile_id AND r.sender_uid=g.uid
            WHERE n.n=1 ORDER BY g.last_event_at DESC,g.profile_id,g.uid,g.anonymous_id
        """, (*parameters, page_size, (page-1)*page_size))
        items = []
        for row in rows:
            item = {key: row[key] for key in ('profile_id','sender_nickname','event_count','post_count','last_event_at','last_discovered_at','risk_evaluated_count')}
            item.update(sender_uid=row['uid'],identity_verified=bool(row['uid']),
                        status_counts=json.loads(row['status_counts']),event_type_counts=json.loads(row['event_type_counts']))
            if row['anonymous_id']:
                item['event_id'] = row['anonymous_id']
            snapshot_row = {key: row[key] for key in ('score','manual_blocked','note','last_hit_at','created_at','updated_at')} if row['score'] is not None else None
            snapshot = risk_snapshot(row['profile_id'], row['uid'], snapshot_row, risk_config)
            item.update({key:snapshot[key] for key in ('risk_score','risk_level','manual_blocked','auto_filtered','owner_exempt','has_record')})
            items.append(item)
        return {'group_by':'uid','items':items,'total':count[0],'page':page,'page_size':page_size}

    async def create_feed_candidate(
        self,
        profile_id: str,
        post_id: str,
        title: str,
        author_uid: str,
        text: str,
        reason: str,
        incoming_event_id: int | None = None,
    ) -> int | None:
        cursor = await self.db.execute(
            """
            INSERT OR IGNORE INTO feed_candidates(
                profile_id, post_id, post_title, post_author_uid,
                generated_text, reason, incoming_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                post_id,
                title,
                str(author_uid),
                text,
                reason,
                incoming_event_id,
                time.time(),
            ),
        )
        return int(cursor.lastrowid) if cursor.rowcount == 1 else None

    async def proactive_event_id(self, profile_id: str, post_id: str) -> int | None:
        row = await self.db.fetchone(
            """
            SELECT id FROM incoming_events
            WHERE profile_id = ? AND post_id = ? AND event_type = 'proactive_feed'
            ORDER BY id DESC
            LIMIT 1
            """,
            (profile_id, post_id),
        )
        return int(row["id"]) if row else None

    async def list_feed_candidates(
        self, *, status: str = "pending", limit: int = 100
    ) -> list[dict[str, Any]]:
        allowed = {
            "pending",
            "approved",
            "sending",
            "rejected",
            "sent",
            "expired",
            "failed",
            "send_unknown",
        }
        if status not in allowed:
            raise ValueError("无效候选状态")
        rows = await self.db.fetchall(
            """
            SELECT * FROM feed_candidates
            WHERE status = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (status, max(1, min(limit, 500))),
        )
        return [dict(row) for row in rows]

    async def feed_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM feed_candidates WHERE id = ?", (candidate_id,))
        return dict(row) if row else None

    async def review_feed_candidate(
        self, candidate_id: int, status: str, edited_text: str | None = None
    ) -> bool:
        if status not in {
            "approved",
            "rejected",
            "sent",
            "failed",
            "expired",
            "send_unknown",
        }:
            raise ValueError("无效审核状态")
        cursor = await self.db.execute(
            """
            UPDATE feed_candidates
            SET status = ?, edited_text = COALESCE(?, edited_text), reviewed_at = ?
            WHERE id = ? AND status IN ('pending', 'approved')
            """,
            (status, edited_text, time.time(), candidate_id),
        )
        return cursor.rowcount == 1

    async def reject_feed_candidate(self, candidate_id: int) -> bool:
        cursor = await self.db.execute(
            """
            UPDATE feed_candidates
            SET status = 'rejected', reviewed_at = ?
            WHERE id = ? AND status IN ('pending', 'approved', 'send_unknown')
            """,
            (time.time(), candidate_id),
        )
        return cursor.rowcount == 1

    async def claim_feed_candidate_for_send(
        self,
        candidate_id: int,
        edited_text: str,
    ) -> dict[str, Any] | None:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE feed_candidates
                SET status = 'sending', edited_text = ?, reviewed_at = ?
                WHERE id = ? AND status IN ('pending', 'approved')
                """,
                (edited_text, time.time(), candidate_id),
            )
            if cursor.rowcount != 1:
                return None
            result = await connection.execute(
                "SELECT * FROM feed_candidates WHERE id = ?",
                (candidate_id,),
            )
            row = await result.fetchone()
            return dict(row) if row is not None else None

    async def finish_feed_candidate_send(
        self,
        candidate_id: int,
        status: str,
        edited_text: str,
        *,
        sent_comment_id: str = "",
    ) -> bool:
        if status not in {"sent", "failed", "send_unknown"}:
            raise ValueError("无效候选发送终态")
        cursor = await self.db.execute(
            """
            UPDATE feed_candidates
            SET status = ?, edited_text = ?, reviewed_at = ?,
                sent_comment_id = CASE WHEN ? != '' THEN ? ELSE sent_comment_id END
            WHERE id = ? AND status = 'sending'
            """,
            (
                status,
                edited_text,
                time.time(),
                sent_comment_id,
                sent_comment_id,
                candidate_id,
            ),
        )
        return cursor.rowcount == 1

    async def recover_interrupted_feed_sends(self) -> int:
        cursor = await self.db.execute(
            """
            UPDATE feed_candidates
            SET status = 'send_unknown', reviewed_at = ?
            WHERE status = 'sending'
            """,
            (time.time(),),
        )
        return max(0, cursor.rowcount)

    async def cleanup_preview(
        self, retention: dict[str, Any], *, now: float | None = None
    ) -> dict[str, int]:
        current = now if now is not None else time.time()
        rules = self._cleanup_rules(retention, current)
        counts: dict[str, int] = {}
        for name, sql, parameters in rules:
            row = await self.db.fetchone(f"SELECT COUNT(*) AS count FROM ({sql})", parameters)
            counts[name] = int(row["count"]) if row else 0
        return counts

    async def cleanup(
        self,
        retention: dict[str, Any],
        *,
        batch_size: int = 500,
        now: float | None = None,
    ) -> dict[str, int]:
        current = now if now is not None else time.time()
        safe_batch = max(1, min(batch_size, 500))
        removed: dict[str, int] = {}
        for name, select_sql, parameters in self._cleanup_rules(retention, current):
            total = 0
            for _ in range(MAX_CLEANUP_BATCHES_PER_RULE):
                cursor = await self.db.execute(
                    f"DELETE FROM {name} WHERE rowid IN (SELECT rowid FROM ({select_sql}) LIMIT ?)",
                    (*parameters, safe_batch),
                )
                changed = max(0, cursor.rowcount)
                total += changed
                if changed < safe_batch:
                    break
            removed[name] = total
        await self.db.checkpoint()
        await self.db.incremental_vacuum()
        return removed

    async def enforce_soft_limit(
        self,
        retention: dict[str, Any],
        *,
        batch_size: int = 500,
    ) -> dict[str, int]:
        soft_limit = int(retention["database_soft_limit_mb"]) * 1024 * 1024
        if self.database_size() <= soft_limit:
            return {}
        safe_batch = max(1, min(batch_size, 500))
        operations = (
            (
                'user_risk_events',
                "DELETE FROM user_risk_events WHERE id IN (SELECT id FROM user_risk_events "
                "WHERE created_at < CAST(strftime('%s','now') AS REAL)-86400 ORDER BY created_at,id LIMIT ?)",
            ),
            (
                "visual_contexts",
                """
                DELETE FROM visual_contexts
                WHERE id IN (
                    SELECT id FROM visual_contexts
                    ORDER BY expires_at ASC, id ASC LIMIT ?
                )
                """,
            ),
            (
                "rejected_feed_candidates",
                """
                DELETE FROM feed_candidates
                WHERE id IN (
                    SELECT id FROM feed_candidates
                    WHERE status IN ('rejected', 'expired')
                    ORDER BY created_at ASC LIMIT ?
                )
                """,
            ),
            (
                "dry_run_bodies",
                """
                UPDATE incoming_events SET content = NULL, raw_json = NULL
                WHERE id IN (
                    SELECT id FROM incoming_events
                    WHERE status = 'dry_run' AND (content IS NOT NULL OR raw_json IS NOT NULL)
                    ORDER BY completed_at ASC LIMIT ?
                )
                """,
            ),
            (
                "historical_event_bodies",
                """
                UPDATE incoming_events SET content = NULL, raw_json = NULL
                WHERE id IN (
                    SELECT id FROM incoming_events
                    WHERE status IN ('sent', 'ignored', 'dead_letter')
                      AND (content IS NOT NULL OR raw_json IS NOT NULL)
                    ORDER BY completed_at ASC LIMIT ?
                )
                """,
            ),
            (
                "successful_reply_bodies",
                """
                UPDATE outgoing_replies SET content = NULL
                WHERE id IN (
                    SELECT id FROM outgoing_replies
                    WHERE status = 'sent' AND content IS NOT NULL
                    ORDER BY confirmed_at ASC LIMIT ?
                )
                """,
            ),
        )
        changed: dict[str, int] = {}
        for name, sql in operations:
            cursor = await self.db.execute(sql, (safe_batch,))
            changed[name] = max(0, cursor.rowcount)
        await self.db.checkpoint()
        await self.db.incremental_vacuum()
        return changed

    def _cleanup_rules(
        self, retention: dict[str, Any], now: float
    ) -> list[tuple[str, str, tuple[Any, ...]]]:
        day = 86400
        failure_cutoff = now - int(retention["failed_days"]) * day
        session_cutoff = now - int(retention["session_mapping_days"]) * day
        dedup_cutoff = now - int(retention["dedup_days"]) * day
        dedup_day = datetime.fromtimestamp(dedup_cutoff, UTC).date().isoformat()
        today = datetime.fromtimestamp(now, UTC).date().isoformat()
        today_start = datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        error_cutoff = now - 30 * day
        return [
            (
                'user_risk_events',
                'SELECT rowid FROM user_risk_events WHERE created_at < ?',
                (min(dedup_cutoff, now-86400),),
            ),
            (
                'user_risk',
                'SELECT rowid FROM user_risk WHERE score=0 AND manual_blocked=0 AND updated_at < ?',
                (min(dedup_cutoff, now-86400),),
            ),
            (
                "visual_contexts",
                "SELECT rowid FROM visual_contexts WHERE expires_at <= ?",
                (now,),
            ),
            (
                "processed_event_keys",
                "SELECT rowid FROM processed_event_keys WHERE created_at < ?",
                (dedup_cutoff,),
            ),
            (
                "session_mappings",
                "SELECT rowid FROM session_mappings WHERE last_used_at < ?",
                (session_cutoff,),
            ),
            (
                "runtime_errors",
                "SELECT rowid FROM runtime_errors WHERE created_at < ?",
                (error_cutoff,),
            ),
            (
                "feed_candidates",
                """
                SELECT rowid FROM feed_candidates
                WHERE status IN ('sent', 'rejected', 'expired', 'failed')
                  AND created_at < ?
                """,
                (dedup_cutoff,),
            ),
            (
                "reply_admissions",
                """
                SELECT rowid FROM reply_admissions
                WHERE day < ? AND updated_at < ?
                  AND outcome IN ('sent', 'failed', 'rejected', 'ignored', 'dead_letter', 'dry_run')
                  AND (floor_active = 0 OR outcome = 'sent')
                """,
                (today, dedup_cutoff),
            ),
            (
                "outgoing_replies",
                """
                SELECT rowid FROM outgoing_replies
                WHERE ((status IN ('failed','retry_wait') AND attempted_at < ?)
                   OR (status IN ('sent', 'dry_run') AND attempted_at < ?))
                  AND attempted_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM reply_admissions a
                      WHERE a.incoming_event_id = outgoing_replies.incoming_event_id
                        AND a.outcome IN ('pending', 'sending', 'send_unknown', 'execution_unknown')
                  )
                """,
                (failure_cutoff, dedup_cutoff, today_start),
            ),
            (
                "incoming_events",
                """
                SELECT rowid FROM incoming_events
                WHERE (
                    (status = 'dead_letter' AND completed_at < ?)
                    OR (status IN ('sent', 'dry_run', 'ignored') AND completed_at < ?)
                )
                  AND (risk_behavior_eligible = 0 OR discovered_at < ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM incoming_events pending
                      WHERE pending.profile_id=incoming_events.profile_id
                        AND pending.sender_uid=incoming_events.sender_uid
                        AND pending.risk_state='pending' AND pending.risk_behavior_eligible=1
                        AND incoming_events.risk_behavior_eligible=1
                        AND pending.discovered_at>=incoming_events.discovered_at
                        AND pending.discovered_at<incoming_events.discovered_at+86400
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM reply_admissions
                      WHERE reply_admissions.incoming_event_id = incoming_events.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM outgoing_replies
                      WHERE outgoing_replies.incoming_event_id = incoming_events.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM feed_candidates
                      WHERE feed_candidates.incoming_event_id = incoming_events.id
                  )
                """,
                (failure_cutoff, dedup_cutoff, now-86400),
            ),
            (
                "self_comment_ids",
                "SELECT rowid FROM self_comment_ids WHERE created_at < ?",
                (dedup_cutoff,),
            ),
            (
                "daily_counters",
                "SELECT rowid FROM daily_counters WHERE day < ?",
                (dedup_day,),
            ),
        ]

    async def prune_event_bodies(
        self, retention: dict[str, Any], *, now: float | None = None
    ) -> int:
        current = now if now is not None else time.time()
        incoming_cutoff = current - int(retention["incoming_body_days"]) * 86400
        dry_run_cutoff = current - int(retention["dry_run_days"]) * 86400
        reply_cutoff = current - int(retention["success_reply_days"]) * 86400
        incoming_cursor = await self.db.execute(
            """
            UPDATE incoming_events
            SET content = NULL, raw_json = NULL
            WHERE completed_at < ? AND status IN ('sent', 'ignored')
              AND (content IS NOT NULL OR raw_json IS NOT NULL)
            """,
            (incoming_cutoff,),
        )
        dry_run_cursor = await self.db.execute(
            """
            UPDATE incoming_events
            SET content = NULL, raw_json = NULL
            WHERE completed_at < ? AND status = 'dry_run'
              AND (content IS NOT NULL OR raw_json IS NOT NULL)
            """,
            (dry_run_cutoff,),
        )
        replies_cursor = await self.db.execute(
            """
            UPDATE outgoing_replies
            SET content = NULL
            WHERE confirmed_at < ? AND status = 'sent' AND content IS NOT NULL
            """,
            (reply_cutoff,),
        )
        return sum(
            max(0, cursor.rowcount) for cursor in (incoming_cursor, dry_run_cursor, replies_cursor)
        )

    async def table_counts(self) -> dict[str, int]:
        tables = (
            "incoming_events",
            "reply_admissions",
            "user_risk",
            "user_risk_events",
            "processed_event_keys",
            "outgoing_replies",
            "self_comment_ids",
            "session_mappings",
            "feed_candidates",
            "runtime_errors",
            "daily_counters",
            "notification_cursors",
            "notification_backfills",
            "visual_contexts",
            "visual_context_event_links",
            "visual_context_comment_links",
        )
        counts: dict[str, int] = {}
        for table in tables:
            row = await self.db.fetchone(f"SELECT COUNT(*) AS count FROM {table}")
            counts[table] = int(row["count"]) if row else 0
        return counts

    def database_size(self) -> int:
        paths = (
            self.db.path,
            Path(f"{self.db.path}-wal"),
            Path(f"{self.db.path}-shm"),
        )
        return sum(path.stat().st_size for path in paths if path.exists())

    async def diagnostic_snapshot(self) -> dict[str, Any]:
        states = await self.db.fetchall(
            """
            SELECT profile_id, status, uid, nickname, login_at, last_login_check_at,
                   last_poll_at, last_success_request_at, last_error,
                   consecutive_401, consecutive_403, consecutive_429,
                   consecutive_poll_failures, circuit_open_until, updated_at
            FROM account_state
            """
        )
        errors = await self.db.fetchall(
            """
            SELECT profile_id, category, message, created_at
            FROM runtime_errors ORDER BY created_at DESC LIMIT 50
            """
        )
        return {
            "schema_version": await self.db.schema_version(),
            "database_size": self.database_size(),
            "counts": await self.table_counts(),
            "account_states": [dict(row) for row in states],
            "recent_errors": [dict(row) for row in errors],
        }


def _content_hash(content: str | None) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()
