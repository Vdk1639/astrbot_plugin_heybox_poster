"""帖子和评论的持久审核队列；只保存业务数据，不保存登录凭据。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any


_STATUSES = ("pending", "sending", "sent", "send_unknown", "failed", "rejected")
_CREDENTIAL_FIELDS = {
    "cookie", "cookies", "set_cookie", "authorization", "token", "access_token",
    "refresh_token", "user_pkey", "pkey", "x_xhh_tokenid",
}


def _encode_json(value: Any) -> str:
    def check_fields(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("业务数据的 JSON 键必须是字符串")
                if key.lower().replace("-", "_") in _CREDENTIAL_FIELDS:
                    raise ValueError("审核队列不能保存登录凭据")
                check_fields(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check_fields(child)

    try:
        check_fields(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("审核数据必须是有效 JSON 业务数据，且不能含登录凭据") from None


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["payload"] = json.loads(record["payload"])
    record["result"] = json.loads(record["result"]) if record["result"] is not None else None
    return record


class ReviewQueue:
    """独立 SQLite 审核库；所有实例应在开始领取任务前初始化。

    打开数据库会将遗留 sending 转成 send_unknown，不允许自动重试。
    每个线程使用自己的实例；写操作通过 SQLite 事务和状态条件串行化。
    """

    def __init__(self, path: str, dedup_seconds: int = 600):
        self.path = str(path)
        self.dedup_seconds = int(dedup_seconds)
        if self.dedup_seconds < 0:
            raise ValueError("去重时间不能为负数")
        self._conn = sqlite3.connect(self.path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            with self._conn:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS reviews (
                        id INTEGER PRIMARY KEY,
                        kind TEXT NOT NULL CHECK(kind IN ('post', 'comment')),
                        profile_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT '',
                        source_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL CHECK(status IN
                            ('pending', 'sending', 'sent', 'send_unknown', 'failed', 'rejected')),
                        result TEXT,
                        error TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS reviews_content "
                    "ON reviews(profile_id, account_id, kind, fingerprint, status)"
                )
                # 多个来源事件可复用同一审核项，仍须分别记住它们的幂等标识。
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS review_events (
                        kind TEXT NOT NULL,
                        profile_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        review_id INTEGER NOT NULL REFERENCES reviews(id),
                        PRIMARY KEY(kind, profile_id, account_id, source, source_id)
                    )
                    """
                )
                self._conn.execute(
                    "UPDATE reviews SET status='send_unknown', updated_at=? WHERE status='sending'",
                    (time.time(),),
                )
        except Exception:
            self._conn.close()
            raise

    def submit(
        self, kind: str, profile_id: str, account_id: str, payload: dict[str, Any],
        *, source: str = "", source_id: str = "",
    ) -> dict[str, Any]:
        if kind not in ("post", "comment"):
            raise ValueError("审核类型只能是 post 或 comment")
        if not all(isinstance(value, str) and value.strip() for value in (profile_id, account_id)):
            raise ValueError("档案和账号标识必须是非空字符串")
        if not isinstance(source, str) or not isinstance(source_id, str):
            raise ValueError("来源和来源事件标识必须是字符串")
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 dict")
        encoded = _encode_json(payload)
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        event = (kind, profile_id, account_id, source, source_id)
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            if source_id:
                row = self._conn.execute(
                    "SELECT r.* FROM review_events e JOIN reviews r ON r.id=e.review_id "
                    "WHERE e.kind=? AND e.profile_id=? AND e.account_id=? AND e.source=? AND e.source_id=?",
                    event,
                ).fetchone()
                if row is not None:
                    return _decode_row(row)
            row = self._conn.execute(
                "SELECT * FROM reviews WHERE kind=? AND profile_id=? AND account_id=? "
                "AND fingerprint=? AND (status IN ('pending', 'sending', 'send_unknown') "
                "OR (status='sent' AND updated_at>?)) ORDER BY id DESC LIMIT 1",
                (kind, profile_id, account_id, fingerprint, now - self.dedup_seconds),
            ).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    "INSERT INTO reviews "
                    "(kind, profile_id, account_id, fingerprint, payload, source, source_id, "
                    "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (kind, profile_id, account_id, fingerprint, encoded, source, source_id, now, now),
                )
                row = self._conn.execute("SELECT * FROM reviews WHERE id=?", (cursor.lastrowid,)).fetchone()
            if source_id:
                self._conn.execute(
                    "INSERT INTO review_events (kind, profile_id, account_id, source, source_id, review_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (*event, row["id"]),
                )
            return _decode_row(row)

    def list(self, kind: str = "", status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if kind and kind not in ("post", "comment"):
            raise ValueError("审核类型只能是 post 或 comment")
        if status and status not in _STATUSES:
            raise ValueError("无效的审核状态")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit 必须是非负整数")
        clauses, values = [], []
        if kind:
            clauses.append("kind=?")
            values.append(kind)
        if status:
            clauses.append("status=?")
            values.append(status)
        query = "SELECT * FROM reviews"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        rows = self._conn.execute(query + " ORDER BY id DESC LIMIT ?", (*values, limit)).fetchall()
        return [_decode_row(row) for row in rows]

    def get(self, review_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        return _decode_row(row) if row is not None else None

    def claim(self, review_id: int, *, profile_id: str, account_id: str) -> dict[str, Any]:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE reviews SET status='sending', updated_at=? "
                "WHERE id=? AND status='pending' AND profile_id=? AND account_id=?",
                (time.time(), review_id, profile_id, account_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("审核项不是待审核状态，或档案、账号不匹配")
            return self.get(review_id)

    def finish(
        self, review_id: int, status: str, *, result: Any = None, error: str = "",
    ) -> dict[str, Any]:
        if status not in ("sent", "send_unknown", "failed"):
            raise ValueError("发送结果只能是 sent、send_unknown 或 failed")
        encoded = _encode_json(result) if result is not None else None
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE reviews SET status=?, result=?, error=?, updated_at=? WHERE id=? AND status='sending'",
                (status, encoded, error, time.time(), review_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("仅发送中的审核项可以记录发送结果")
            return self.get(review_id)

    def reject(self, review_id: int) -> dict[str, Any]:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE reviews SET status='rejected', updated_at=? WHERE id=? AND status='pending'",
                (time.time(), review_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("仅待审核的项目可以拒绝")
            return self.get(review_id)

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(_STATUSES, 0)
        counts.update(self._conn.execute("SELECT status, COUNT(*) FROM reviews GROUP BY status").fetchall())
        return counts

    def close(self) -> None:
        self._conn.close()
