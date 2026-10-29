"""本地发帖 outbox：发帖前登记，成功落账，超时标记 unknown，供对账认领。

只依赖标准库 ``sqlite3``。表结构：

* ``fingerprint``：内容幂等指纹（主键）
* ``payload``：发送时的表单与对账元数据（JSON）
* ``status``：``pending`` / ``sent`` / ``send_unknown``
* ``link_id``：确认成功后回填
* ``created_at`` / ``updated_at``：时间戳
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any


class Outbox:
    """SQLite 落地的发帖 outbox。"""

    def __init__(self, path: str, dedup_seconds: int = 600):
        self.path = str(path)
        self.dedup_seconds = int(dedup_seconds)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, timeout=30)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                fingerprint TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                link_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def mark_pending(self, fingerprint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """发送前登记；同一指纹在去重窗口内已 ``sent`` 则直接返回重复标记。"""
        conn = self._get_conn()
        now = time.time()
        row = conn.execute(
            "SELECT status, link_id, updated_at FROM outbox WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        # 以 updated_at 为基准：mark_sent 会刷新它，避免窗口过期后
        # created_at 停留不动导致同内容可无限重发。
        if row and row[0] == "sent" and (now - row[2]) < self.dedup_seconds:
            return {"duplicate": True, "link_id": row[1]}
        conn.execute(
            """
            INSERT INTO outbox
                (fingerprint, payload, status, link_id, created_at, updated_at)
            VALUES (?, ?, 'pending', '', ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                payload=excluded.payload,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (fingerprint, json.dumps(payload, ensure_ascii=False), now, now),
        )
        conn.commit()
        return {"duplicate": False, "link_id": ""}

    def mark_sent(self, fingerprint: str, link_id: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE outbox SET status='sent', link_id=?, updated_at=? WHERE fingerprint=?",
            (str(link_id or ""), time.time(), fingerprint),
        )
        conn.commit()

    def mark_unknown(self, fingerprint: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE outbox SET status='send_unknown', updated_at=? WHERE fingerprint=?",
            (time.time(), fingerprint),
        )
        conn.commit()

    def mark_failed(self, fingerprint: str) -> None:
        """明确失败（服务端已拒绝、未创建）时标记，避免残留 pending。"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE outbox SET status='failed', updated_at=? WHERE fingerprint=?",
            (time.time(), fingerprint),
        )
        conn.commit()

    def unknown_rows(self) -> list[tuple[str, str]]:
        """返回所有 ``send_unknown`` 的 ``(fingerprint, payload_json)``。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT fingerprint, payload FROM outbox "
            "WHERE status='send_unknown' ORDER BY created_at ASC"
        ).fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]

    def pending_rows(self) -> list[str]:
        """返回所有 ``pending`` 状态的 fingerprint。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT fingerprint FROM outbox "
            "WHERE status='pending' ORDER BY created_at ASC"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def remove_by_link_id(self, link_id: str) -> int:
        """删除与指定 link_id 关联的 outbox 记录，返回删除行数。"""
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM outbox WHERE link_id=?",
            (str(link_id or ""),),
        )
        conn.commit()
        return cur.rowcount
